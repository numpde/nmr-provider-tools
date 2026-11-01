"""Raw kernel evidence is distinct from a host-coordinate conversion value."""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from linux_idmap import IdRange, Maps, parse_map
from linux_idmap.addons.errors import IdentityError
from linux_idmap.addons.host import HostMap, ProcessIdentity, consistent_maps
from linux_idmap.addons.observation import observe_host_process
from linux_idmap.addons.probe import ProbeResult


class MapBoundaryTests(unittest.TestCase):
    def test_raw_rows_have_no_translation_claim(self):
        mapping = parse_map(b"1000 4294967295 1\n")
        self.assertEqual((IdRange(1000, 4294967295, 1),), mapping)
        self.assertFalse(hasattr(mapping, "translate"))

    def test_malformed_rows_keep_normalization_contract(self):
        for raw in (b"0 1\n", b"0 1 2 3\n", b"bad 1 2\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_map(raw)
                with self.assertRaises(IdentityError) as caught:
                    consistent_maps(lambda _: raw)
                self.assertEqual("process-identity", caught.exception.category)

    def test_repeated_raw_rows_do_not_earn_host_coordinates(self):
        maps = consistent_maps(lambda _: b"0 4294967295 1\n")
        self.assertEqual((IdRange(0, 4294967295, 1),), maps.uid_map)
        self.assertFalse(hasattr(maps.uid_map, "translate"))

    def test_synthetic_host_map_fixture_preserves_arithmetic_and_json(self):
        # Deliberately constructed fixture: this is not evidence of provenance.
        mapping = HostMap((IdRange(0, 1000, 1), IdRange(1, 100000, 3)))
        self.assertEqual((1000, 100000, 100001, 100002),
                         tuple(mapping.translate(x) for x in range(4)))
        for x in (4, -1, True, 1.0, 2**32 - 1):
            with self.subTest(x=x), self.assertRaises(ValueError):
                mapping.translate(x)
        payload = json.loads(json.dumps(ProbeResult(mapping, mapping, "fake").as_dict()))
        self.assertEqual([{"inside": 0, "outside": 1000, "length": 1},
                          {"inside": 1, "outside": 100000, "length": 3}], payload["uid_map"])

    def test_qualified_value_equality_is_not_raw_evidence_equality(self):
        raw = parse_map(b"0 1000 1\n")
        qualified = HostMap(raw)
        self.assertNotEqual(raw, qualified)
        self.assertEqual(qualified, HostMap(raw))
        self.assertEqual(tuple(asdict(row) for row in raw), asdict(qualified)["rows"])

    def test_only_successful_qualified_observation_delivers_host_map(self):
        raw = Maps(parse_map(b"0 1000 1\n"), parse_map(b"0 2000 1\n"))
        identity = ProcessIdentity(42, 123, 7, 9)
        source = SimpleNamespace(identity=identity, read_maps=lambda: raw)

        @contextmanager
        def succeeds(_pid):
            yield source

        with patch("linux_idmap.addons.observation.qualified_process", succeeds):
            observed = observe_host_process(42)
        self.assertIsInstance(observed.uid_map, HostMap)
        self.assertEqual(1000, observed.uid_map.translate(0))
        self.assertEqual(raw.uid_map, observed.uid_map.rows)

        @contextmanager
        def exit_fails(_pid):
            yield source
            raise IdentityError("process-identity", "source changed before release")

        with patch("linux_idmap.addons.observation.qualified_process", exit_fails):
            with self.assertRaises(IdentityError):
                observe_host_process(42)

    def test_identical_printed_rows_do_not_determine_reader_translation(self):
        # Kernel-source model, NOT a real privileged sibling-namespace test.
        # An initial-user parent may legally install target (0, 500, 5),
        # reader A (1000, 500, 5), or reader B (1000, 500, 2)/(2000, 502, 3).
        # Each map is positive and disjoint in both domains, with parent coverage.
        # uid_m_show translates lower_first only, not each ID in the extent:
        # https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L630
        target_to_global = dict(zip(range(5), range(500, 505)))
        reader_a = dict(zip(range(500, 505), range(1000, 1005)))
        reader_b = {500: 1000, 501: 1001, 502: 2000, 503: 2001, 504: 2002}
        def printed_row(reader):
            return f"0 {reader[target_to_global[0]]} 5\n".encode()
        self.assertEqual(printed_row(reader_a), printed_row(reader_b))
        self.assertEqual(1002, reader_a[target_to_global[2]])
        self.assertEqual(2000, reader_b[target_to_global[2]])

    def test_real_isolated_unmapped_reader_preserves_sentinel_without_translation(self):
        child = r'''
import ctypes, errno, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from linux_idmap import observe_process, parse_map
from linux_idmap.addons.observation import observe_host_process
from linux_idmap.addons.errors import IdentityError
parent = os.getppid()
old_namespace = os.stat("/proc/self/ns/user").st_ino
libc = ctypes.CDLL(None, use_errno=True)
libc.unshare.argtypes = [ctypes.c_int]
libc.unshare.restype = ctypes.c_int
if libc.unshare(0x10000000) != 0:
    error = ctypes.get_errno()
    print(json.dumps({'errno': error, 'reason': os.strerror(error)}))
    raise SystemExit(77 if error in (errno.EPERM, errno.ENOSYS) else 1)
assert os.stat('/proc/self/ns/user').st_ino != old_namespace
assert Path('/proc/self/uid_map').read_bytes() == b''
assert Path('/proc/self/gid_map').read_bytes() == b''
assert observe_process(os.getpid()).uid_map == ()
raw = Path(f'/proc/{parent}/uid_map').read_bytes()
mapping = parse_map(raw)
assert mapping
assert all(row.outside == 2**32 - 1 for row in mapping)
assert observe_process(parent).uid_map == mapping
assert not hasattr(mapping, 'translate')
try:
    observe_host_process(parent)
except IdentityError as error:
    assert error.category == 'host-precondition'
else:
    raise AssertionError('Unmapped reader was host-qualified')
print(json.dumps({'raw': raw.decode(), 'raw_has_translation': hasattr(mapping, 'translate')}))
'''
        import linux_idmap
        source = str(Path(linux_idmap.__file__).parent.parent)
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", child, source],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 77:
            import errno
            evidence = json.loads(result.stdout)
            self.assertIn(evidence["errno"], (errno.EPERM, errno.ENOSYS))
            self.skipTest(f"unshare unavailable: {evidence}")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["raw_has_translation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
