"""Host-namespace and process binding contract, without a Docker daemon."""

from __future__ import annotations

from dataclasses import replace
import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from linux_idmap._files import FileIdentity
from linux_idmap import IdMap
from linux_idmap._procfs import ReaderEvidence, ProcessStatus, _directory, _read_at
from linux_idmap._errors import IdentityError
from linux_idmap._probe_process import ProbeProcesses


class StatTests(unittest.TestCase):
    def test_comm_can_contain_spaces_and_closing_parentheses(self):
        raw = b"44 (strange ) process)) S " + b"0 " * 18 + b"321 0\n"
        self.assertEqual(ProcessStatus("S", 321), ProcessStatus.of(raw, 44))

    def test_wrong_pid_bad_framing_and_invalid_starttime_rejected(self):
        valid = b"44 (probe) S " + b"0 " * 18 + b"321\n"
        for raw in (valid.replace(b"44", b"45", 1), b"44 probe S 1", valid.replace(b"321", b"-1")):
            with self.subTest(raw=raw), self.assertRaises(IdentityError):
                ProcessStatus.of(raw, 44)

    def test_only_reported_exit_states_count_as_exit(self):
        for state in ('R', 'S', 'D', 'T', 't', 'W', 'I', 'K', 'P', 'Z', 'X', 'x'):
            raw = b'44 (probe) ' + state.encode() + b' ' + b'0 ' * 18 + b'321\n'
            status = ProcessStatus.of(raw, 44)
            self.assertEqual(state in 'ZXx', status.exited)
            self.assertEqual(state not in 'ZXx', status.live)
        for state in (b'?', b'', b'RS', b'\xff'):
            raw = b'44 (probe) ' + state + b' ' + b'0 ' * 18 + b'321\n'
            with self.subTest(state=state), self.assertRaises(IdentityError):
                ProcessStatus.of(raw, 44)


class HostGuardTests(unittest.TestCase):
    def test_empty_explicit_map_rejects_unmapped_or_invalid_ids(self):
        mapping = IdMap(())
        for identifier in (0, -1, True, 1.0, '1', 2**32 - 1):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                mapping.translate(identifier)

    def test_genuine_proc_read_overflow_closes_its_file_descriptor(self):
        with _directory(Path('/proc') / str(os.getpid())) as directory:
            with patch('linux_idmap._procfs.os.close', wraps=os.close) as close:
                with self.assertRaisesRegex(IdentityError, 'exceeds its read bound') as caught:
                    _read_at(directory, 'cmdline', 1)
            self.assertEqual('process-identity', caught.exception.category)
            close.assert_called_once()
            with self.assertRaises(OSError) as closed:
                os.fstat(close.call_args.args[0])
            self.assertEqual(errno.EBADF, closed.exception.errno)
            os.fstat(directory)  # The caller's pinned directory still belongs to its scope.

    def test_regular_directory_masquerading_as_proc_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(IdentityError) as caught:
                ProbeProcesses(Path(directory)).reader_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_identity_looking_maps_do_not_override_nested_namespace(self):
        guard = ProbeProcesses()
        with patch("linux_idmap._procfs._namespace_inode", side_effect=[4026531838, 4026531836]):
            with self.assertRaises(IdentityError) as caught:
                guard.reader_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_nested_pid_namespace_rejected_even_if_user_initial(self):
        with patch("linux_idmap._procfs._namespace_inode", side_effect=[4026531837, 4026531839]):
            with self.assertRaises(IdentityError):
                ProbeProcesses().reader_evidence()

    def test_initial_inode_requires_genuine_nsfs(self):
        with patch("linux_idmap._procfs._filesystem_type", side_effect=[0x9FA0, 0x1234]):
            with self.assertRaises(IdentityError) as caught:
                ProbeProcesses().reader_evidence()
        self.assertEqual("host-precondition", caught.exception.category)

    def test_wrong_proc_self_visibility_rejected(self):
        with patch("linux_idmap._procfs.os.readlink", return_value="999999999"):
            with self.assertRaises(IdentityError):
                ProbeProcesses().reader_evidence()


class RealProcTests(unittest.TestCase):
    def setUp(self):
        self.token = "owned-probe-token-123456789"
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c",
             "import time; print('ready', flush=True); time.sleep(20)", self.token], stdout=subprocess.PIPE)
        self.addCleanup(self.stop_process)
        self.addCleanup(self.process.stdout.close)
        self.assertTrue(select.select([self.process.stdout], [], [], 5)[0], "child did not become ready")
        self.assertEqual(b"ready\n", self.process.stdout.readline())
        self.guard = ProbeProcesses()
        proc = Path("/proc").stat()
        self.host = ReaderEvidence(4026531837, FileIdentity.of(proc),
                                 os.getpid(), threading.get_native_id())
        # Container CI lacks the required initial namespaces. This fixture mocks
        # only the guard; all process/map reads below use the genuine /proc.
        self.enterContext(patch.object(self.guard, "reader_evidence", return_value=self.host))

    def stop_process(self):
        self.process.terminate()
        self.process.wait(timeout=3)

    def test_live_process_exact_token_maps_and_retirement(self):
        observed = self.guard.observe(self.process.pid, self.token)
        self.assertEqual(self.process.pid, observed.identity.pid)
        self.assertGreater(observed.identity.starttime, 0)
        self.assertGreater(observed.identity.directory.inode, 0)
        self.assertFalse(self.guard.process_retired(observed.identity))
        self.process.terminate()
        self.process.wait(timeout=3)
        self.assertTrue(self.guard.process_retired(observed.identity))

    def test_token_must_be_complete_argv_not_substring(self):
        for token in (self.token[:-1], self.token + "extra"):
            with self.subTest(token=token), self.assertRaises(IdentityError):
                self.guard.observe(self.process.pid, token)

    def test_pid_reuse_counts_as_retired_not_same_probe(self):
        observed = self.guard.observe(self.process.pid, self.token)
        old = replace(observed.identity, starttime=observed.identity.starttime - 1)
        self.assertTrue(self.guard.process_retired(old))

    def test_namespace_change_around_observation_rejected(self):
        with patch.object(self.guard, "reader_evidence", side_effect=[self.host, IdentityError("host-precondition", "changed")]):
            with self.assertRaises(IdentityError):
                self.guard.observe(self.process.pid, self.token)

    def test_map_change_during_observation_rejected(self):
        from linux_idmap import _procfs as linux
        original = linux._read_at
        reads = 0
        def changing(fd, name, limit):
            nonlocal reads
            if name == "uid_map":
                reads += 1
                if reads == 2:
                    return b"0 1000 1\n"
            return original(fd, name, limit)
        with patch("linux_idmap._procfs._read_at", side_effect=changing):
            with self.assertRaises(IdentityError):
                self.guard.observe(self.process.pid, self.token)

    def test_token_change_before_scope_exit_rejects_both_observation_kinds(self):
        from linux_idmap import _procfs as linux
        original = linux._read_at
        for observe in (self.guard.identify, self.guard.observe):
            with self.subTest(observe=observe.__name__):
                token_reads = 0
                def changing(fd, name, limit):
                    nonlocal token_reads
                    if name == 'cmdline':
                        token_reads += 1
                        # Only argv is controlled; process/stat/procfs evidence is real.
                        return self.token.encode() + b'\0' if token_reads == 1 else b'foreign-process\0'
                    return original(fd, name, limit)
                with patch('linux_idmap._procfs._read_at', side_effect=changing) as reads:
                    with self.assertRaises(IdentityError):
                        observe(self.process.pid, self.token)
                token_calls = [call.args for call in reads.call_args_list if call.args[1] == 'cmdline']
                self.assertEqual(2, len(token_calls))
                self.assertEqual(token_calls[0][0], token_calls[1][0])

    def test_unreadable_retirement_is_not_absence(self):
        observed = self.guard.observe(self.process.pid, self.token)
        with patch("linux_idmap._procfs._read_at", side_effect=PermissionError("denied")):
            with self.assertRaises(IdentityError):
                self.guard.process_retired(observed.identity)

    def test_pid_starttime_change_during_observation_rejected(self):
        from linux_idmap import _procfs as linux
        original = linux.ProcessStatus.of
        reads = 0
        def changing(raw, pid):
            nonlocal reads
            reads += 1
            status = original(raw, pid)
            # silent-defaults: allow-contract -- Only the second stat observation advances time to model PID replacement; the first remains unchanged.
            return replace(status, starttime=status.starttime + (1 if reads == 2 else 0))
        with patch("linux_idmap._procfs.ProcessStatus.of", side_effect=changing):
            with self.assertRaises(IdentityError):
                self.guard.observe(self.process.pid, self.token)

    def test_inode_change_alone_does_not_prove_retirement(self):
        observed = self.guard.observe(self.process.pid, self.token)
        other_inode = replace(observed.identity, directory=replace(observed.identity.directory, inode=observed.identity.directory.inode + 1))
        with self.assertRaises(IdentityError):
            self.guard.process_retired(other_inode)

    def test_retirement_rechecks_host_namespace(self):
        observed = self.guard.observe(self.process.pid, self.token)
        with patch.object(self.guard, "reader_evidence", side_effect=[self.host, IdentityError("host-precondition", "changed")]):
            with self.assertRaises(IdentityError):
                self.guard.process_retired(observed.identity)

    def test_each_process_read_rejects_a_replaced_proc_root(self):
        observed = self.guard.observe(self.process.pid, self.token)
        reads = (
            lambda: self.guard.observe(self.process.pid, self.token),
            lambda: self.guard.identify(self.process.pid, self.token),
            lambda: self.guard.pid_absent(self.process.pid),
            lambda: self.guard.process_retired(observed.identity),
        )
        changed = replace(self.host, procfs=replace(self.host.procfs, inode=self.host.procfs.inode + 1))
        for read in reads:
            with self.subTest(read=read), patch.object(self.guard, "reader_evidence", return_value=changed):
                with patch("linux_idmap._procfs._read_at") as read_file:
                    with self.assertRaises(IdentityError) as caught:
                        read()
                    self.assertEqual("host-precondition", caught.exception.category)
                    read_file.assert_not_called()

    def test_boolean_retirement_results_still_recheck_reader_evidence(self):
        observed = self.guard.observe(self.process.pid, self.token)
        reads = (lambda: self.guard.pid_absent(self.process.pid),
                 lambda: self.guard.process_retired(observed.identity))
        changed = replace(self.host, reader_tid=self.host.reader_tid + 1)
        for exited in (False, True):
            if exited:
                self.process.terminate()
                self.process.wait(timeout=3)
            for read in reads:
                with self.subTest(exited=exited, read=read):
                    with patch.object(self.guard, "reader_evidence", side_effect=[self.host, changed]):
                        with self.assertRaises(IdentityError) as caught:
                            read()
                        self.assertEqual("host-precondition", caught.exception.category)

    def test_failed_process_read_keeps_original_error_without_a_postcheck(self):
        changed = IdentityError("host-precondition", "later host failure")
        with patch.object(self.guard, "reader_evidence", side_effect=[self.host, changed]) as host:
            with self.assertRaises(IdentityError) as caught:
                self.guard.observe(self.process.pid, self.token + "wrong")
        self.assertEqual("process-identity", caught.exception.category)
        self.assertEqual(1, host.call_count)

    def test_zombie_process_is_retired_and_cannot_be_observed(self):
        observed = self.guard.observe(self.process.pid, self.token)
        self.process.terminate()
        # waitid WNOWAIT establishes exit but deliberately leaves the zombie.
        os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOWAIT)
        self.assertTrue(self.guard.process_retired(observed.identity))
        with self.assertRaises(IdentityError):
            self.guard.observe(self.process.pid, self.token)


if __name__ == "__main__":
    unittest.main()
