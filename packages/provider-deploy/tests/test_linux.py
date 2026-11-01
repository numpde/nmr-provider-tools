"""Host-namespace and process binding contract, without a Docker daemon."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from provider_deploy.linux import (
    HostEvidence, IdMap, IdRange, LinuxError, LinuxGuard, ProcessObservation,
    parse_id_map, _parse_stat,
)


class MapTests(unittest.TestCase):
    def test_multirange_translation_and_initial_map(self):
        mapping = parse_id_map(b"         0       1000          1\n1 100000 65536\n")
        self.assertEqual(1000, mapping.translate(0))
        self.assertEqual(100999, mapping.translate(1000))
        self.assertEqual(65532, parse_id_map(b"0 0 4294967295\n").translate(65532))
        self.assertEqual((IdRange(0, 1000, 1), IdRange(1, 100000, 65536)), mapping.rows)

    def test_invalid_rows_and_overlap_in_either_domain_fail_closed(self):
        values = [b"", b"0 0 0\n", b"0 0 -1\n", b"+0 0 1\n", b"00 0 1\n",
                  b"0 0 1\n\n", b"0 0 1 extra\n", b"0\v0 1\n", b"0 0 1\r\n", b"0 4294967294 2\n",
                  b"4294967295 0 1\n", b"0 0 4294967296\n",
                  b"0 10 2\n1 20 2\n", b"0 10 2\n2 11 2\n", b"\xff\n"]
        for raw in values:
            with self.subTest(raw=raw), self.assertRaises(LinuxError):
                parse_id_map(raw)

    def test_id_validation_excludes_sentinel_bools_and_unmapped_ids(self):
        mapping = parse_id_map(b"0 1000 1\n")
        for value in (True, False, -1, 4294967295, 1, "0", 0.0):
            with self.subTest(value=value), self.assertRaises(LinuxError):
                mapping.translate(value)

    def test_row_count_and_byte_bounds(self):
        with self.assertRaises(LinuxError):
            parse_id_map(b"0 0 1\n" * 341)
        with self.assertRaises(LinuxError):
            parse_id_map(b" " * 65537)

    def test_direct_map_construction_validates_not_only_parser(self):
        for rows in ((), (IdRange(0, 0, 0),), (IdRange(True, 0, 1),),
                     (IdRange(0, 0, 2), IdRange(1, 3, 1))):
            with self.subTest(rows=rows), self.assertRaises(LinuxError):
                IdMap(rows)


class StatTests(unittest.TestCase):
    def test_comm_can_contain_spaces_and_closing_parentheses(self):
        raw = b"44 (strange ) process)) S " + b"0 " * 18 + b"321 0\n"
        self.assertEqual(("S", 321), _parse_stat(raw, 44))

    def test_wrong_pid_bad_framing_and_invalid_starttime_rejected(self):
        valid = b"44 (probe) S " + b"0 " * 18 + b"321\n"
        for raw in (valid.replace(b"44", b"45", 1), b"44 probe S 1", valid.replace(b"321", b"-1")):
            with self.subTest(raw=raw), self.assertRaises(LinuxError):
                _parse_stat(raw, 44)


class HostGuardTests(unittest.TestCase):
    def test_regular_directory_masquerading_as_proc_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(LinuxError) as caught:
                LinuxGuard(Path(directory)).host_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_identity_looking_maps_do_not_override_nested_namespace(self):
        guard = LinuxGuard()
        with patch("provider_deploy.linux._namespace_inode", side_effect=[4026531838, 4026531836]):
            with self.assertRaises(LinuxError) as caught:
                guard.host_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_nested_pid_namespace_rejected_even_if_user_initial(self):
        with patch("provider_deploy.linux._namespace_inode", side_effect=[4026531837, 4026531839]):
            with self.assertRaises(LinuxError):
                LinuxGuard().host_evidence()

    def test_initial_inode_requires_genuine_nsfs(self):
        with patch("provider_deploy.linux._filesystem_type", side_effect=[0x9FA0, 0x1234]):
            with self.assertRaises(LinuxError) as caught:
                LinuxGuard().host_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_wrong_proc_self_visibility_rejected(self):
        with patch("provider_deploy.linux.os.readlink", return_value="999999999"):
            with self.assertRaises(LinuxError):
                LinuxGuard().host_evidence()


class RealProcTests(unittest.TestCase):
    def setUp(self):
        self.token = "owned-probe-token-123456789"
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", self.token])
        self.guard = LinuxGuard()
        proc = Path("/proc").stat()
        self.host = HostEvidence(4026531837, 4026531836, proc.st_dev, proc.st_ino,
                                 os.getpid(), threading.get_native_id())
        # Container CI lacks the required initial namespaces. This fixture mocks
        # only the guard; all process/map reads below use the genuine /proc.
        self.guard_patch = patch.object(self.guard, "host_evidence", return_value=self.host)
        self.guard_patch.start()

    def tearDown(self):
        self.guard_patch.stop()
        self.process.terminate()
        self.process.wait(timeout=3)

    def test_live_process_exact_token_maps_and_retirement(self):
        observed = self.guard.observe(self.process.pid, self.token)
        self.assertEqual(self.process.pid, observed.pid)
        self.assertGreater(observed.starttime, 0)
        self.assertGreater(observed.proc_inode, 0)
        self.assertFalse(self.guard.process_retired(observed))
        self.process.terminate()
        self.process.wait(timeout=3)
        self.assertTrue(self.guard.process_retired(observed))

    def test_token_must_be_complete_argv_not_substring(self):
        for token in (self.token[:-1], self.token + "extra"):
            with self.subTest(token=token), self.assertRaises(LinuxError):
                self.guard.observe(self.process.pid, token)

    def test_pid_reuse_counts_as_retired_not_same_probe(self):
        observed = self.guard.observe(self.process.pid, self.token)
        old = ProcessObservation(observed.pid, observed.starttime - 1, observed.proc_device,
                                 observed.proc_inode, observed.uid_map, observed.gid_map)
        self.assertTrue(self.guard.process_retired(old))

    def test_namespace_change_around_observation_rejected(self):
        with patch.object(self.guard, "host_evidence", side_effect=[self.host, LinuxError("host-precondition", "changed")]):
            with self.assertRaises(LinuxError):
                self.guard.observe(self.process.pid, self.token)

    def test_map_change_during_observation_rejected(self):
        from provider_deploy import linux
        original = linux._read_at
        reads = 0
        def changing(fd, name, limit):
            nonlocal reads
            if name == "uid_map":
                reads += 1
                if reads == 2:
                    return b"0 1000 1\n"
            return original(fd, name, limit)
        with patch("provider_deploy.linux._read_at", side_effect=changing):
            with self.assertRaises(LinuxError):
                self.guard.observe(self.process.pid, self.token)

    def test_unreadable_retirement_is_not_absence(self):
        observed = self.guard.observe(self.process.pid, self.token)
        with patch("provider_deploy.linux._read_at", side_effect=PermissionError("denied")):
            with self.assertRaises(LinuxError):
                self.guard.process_retired(observed)

    def test_pid_starttime_change_during_observation_rejected(self):
        from provider_deploy import linux
        original = linux._parse_stat
        reads = 0
        def changing(raw, pid):
            nonlocal reads
            reads += 1
            state, starttime = original(raw, pid)
            # silent-defaults: allow-contract -- Only the second stat observation advances time to model PID replacement; the first remains unchanged.
            return state, starttime + (1 if reads == 2 else 0)
        with patch("provider_deploy.linux._parse_stat", side_effect=changing):
            with self.assertRaises(LinuxError):
                self.guard.observe(self.process.pid, self.token)

    def test_inode_change_alone_does_not_prove_retirement(self):
        observed = self.guard.observe(self.process.pid, self.token)
        other_inode = ProcessObservation(observed.pid, observed.starttime, observed.proc_device,
                                         observed.proc_inode + 1, observed.uid_map, observed.gid_map)
        with self.assertRaises(LinuxError):
            self.guard.process_retired(other_inode)

    def test_retirement_rechecks_host_namespace(self):
        observed = self.guard.observe(self.process.pid, self.token)
        with patch.object(self.guard, "host_evidence", side_effect=[self.host, LinuxError("host-precondition", "changed")]):
            with self.assertRaises(LinuxError):
                self.guard.process_retired(observed)

    def test_each_process_read_rejects_a_replaced_proc_root(self):
        observed = self.guard.observe(self.process.pid, self.token)
        reads = (
            lambda: self.guard.observe(self.process.pid, self.token),
            lambda: self.guard.identify(self.process.pid, self.token),
            lambda: self.guard.pid_absent(self.process.pid),
            lambda: self.guard.process_retired(observed),
        )
        changed = replace(self.host, proc_inode=self.host.proc_inode + 1)
        for read in reads:
            with self.subTest(read=read), patch.object(self.guard, "host_evidence", return_value=changed):
                with patch("provider_deploy.linux._read_at") as read_file:
                    with self.assertRaises(LinuxError) as caught:
                        read()
                    self.assertEqual("host-precondition", caught.exception.category)
                    read_file.assert_not_called()

    def test_boolean_retirement_results_still_recheck_host_evidence(self):
        observed = self.guard.observe(self.process.pid, self.token)
        reads = (lambda: self.guard.pid_absent(self.process.pid),
                 lambda: self.guard.process_retired(observed))
        changed = replace(self.host, reader_tid=self.host.reader_tid + 1)
        for exited in (False, True):
            if exited:
                self.process.terminate()
                self.process.wait(timeout=3)
            for read in reads:
                with self.subTest(exited=exited, read=read):
                    with patch.object(self.guard, "host_evidence", side_effect=[self.host, changed]):
                        with self.assertRaises(LinuxError) as caught:
                            read()
                        self.assertEqual("host-precondition", caught.exception.category)

    def test_failed_process_read_keeps_original_error_without_a_postcheck(self):
        changed = LinuxError("host-precondition", "later host failure")
        with patch.object(self.guard, "host_evidence", side_effect=[self.host, changed]) as host:
            with self.assertRaises(LinuxError) as caught:
                self.guard.observe(self.process.pid, self.token + "wrong")
        self.assertEqual("process-identity", caught.exception.category)
        self.assertEqual(1, host.call_count)

    def test_zombie_process_is_retired_and_cannot_be_observed(self):
        observed = self.guard.observe(self.process.pid, self.token)
        self.process.terminate()
        # waitid WNOWAIT establishes exit but deliberately leaves the zombie.
        os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOWAIT)
        self.assertTrue(self.guard.process_retired(observed))
        with self.assertRaises(LinuxError):
            self.guard.observe(self.process.pid, self.token)


if __name__ == "__main__":
    unittest.main()
