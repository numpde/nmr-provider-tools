"""Raw evidence does not imply affine conversion or host coordinates."""

from contextlib import contextmanager
from dataclasses import asdict
import errno
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from linux_idmap._files import FileIdentity
from linux_idmap import Identifier, IdMap, IdRange, MapRows, parse_map
from linux_idmap._errors import IdentityError
from linux_idmap._procfs import ProcessIdentity, consistent_maps
from linux_idmap.process import observe_host_process
from linux_idmap.probe import ProbeResult


class MapBoundaryTests(unittest.TestCase):
    def test_raw_rows_have_no_translation_claim(self):
        mapping = parse_map(b"1000 4294967295 1\n")
        self.assertEqual([IdRange(1000, 4294967295, 1)], list(mapping))
        self.assertFalse(hasattr(mapping, "translate"))

    def test_malformed_rows_keep_normalization_contract(self):
        for raw in (b"0 1\n", b"0 1 2 3\n", b"bad 1 2\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    list(parse_map(raw))
                with self.assertRaises(IdentityError) as caught:
                    consistent_maps(lambda _: raw)
                self.assertEqual("process-identity", caught.exception.category)

    def test_consistency_consumes_each_sample_inside_the_source_scope(self):
        active = False
        consumed = Mock()

        def rows(value):
            self.assertTrue(active)
            consumed(value)
            yield IdRange(0, value, 1)

        def sample(_read_file):
            return MapRows(rows(1000), rows(2000))

        @contextmanager
        def source():
            nonlocal active
            active = True
            try:
                yield lambda _name: b''
            finally:
                active = False

        with source() as read_file, patch('linux_idmap._procfs.read_maps', sample):
            maps = consistent_maps(read_file)
            self.assertEqual([1000, 2000, 1000, 2000],
                             [call.args[0] for call in consumed.call_args_list])
        self.assertFalse(active)
        mapping = IdMap(maps.uid_rows)
        self.assertEqual([1000, 1000], [mapping.translate(0) for _ in range(2)])
        self.assertEqual([{'inside': 0, 'outside': 1000, 'length': 1}], asdict(mapping)['rows'])

    def test_later_parse_error_is_normalized_before_scope_exit(self):
        closed = Mock()

        @contextmanager
        def source():
            try:
                yield lambda _name: b'0 1000 1\n1 1001\n'
            finally:
                closed()

        with source() as read_file:
            with self.assertRaises(IdentityError) as caught:
                consistent_maps(read_file)
            closed.assert_not_called()
            self.assertIsInstance(caught.exception.__cause__, ValueError)
        closed.assert_called_once_with()

    def test_repeated_raw_rows_do_not_earn_host_coordinates(self):
        maps = consistent_maps(lambda _: b"0 4294967295 1\n")
        self.assertEqual([IdRange(0, 4294967295, 1)], maps.uid_rows)
        self.assertFalse(hasattr(maps.uid_rows, "translate"))

    def test_explicit_affine_map_preserves_arithmetic_and_json(self):
        # Deliberately constructed fixture: this is not evidence of provenance.
        mapping = IdMap((IdRange(0, 1000, 1), IdRange(1, 100000, 3)))
        self.assertEqual([1000, 100000, 100001, 100002],
                         [mapping.translate(x) for x in range(4)])
        for x in (4, -1, True, 1.0, 2**32 - 1):
            with self.subTest(x=x), self.assertRaises(ValueError):
                mapping.translate(x)
        self.assertIsInstance(mapping.translate(Identifier.of("0")), Identifier)
        self.assertEqual("1000", json.dumps(mapping.translate(0)))
        payload = json.loads(json.dumps(ProbeResult(mapping, mapping, "fake").as_dict()))
        self.assertEqual([{"inside": 0, "outside": 1000, "length": 1},
                          {"inside": 1, "outside": 100000, "length": 3}], payload["uid_map"])

    def test_explicit_affine_map_refuses_unusable_output_ids(self):
        for outside in (-1, 2**32 - 1, 2**32, 1.5):
            with self.subTest(outside=outside), self.assertRaises(ValueError):
                IdMap((IdRange(0, outside, 1),)).translate(0)
        with self.assertRaises(ValueError):
            IdMap((IdRange(0, 2**32 - 2, 2),)).translate(1)
        self.assertEqual(2**32 - 2, IdMap((IdRange(0, 2**32 - 2, 1),)).translate(0))

    def test_affine_value_equality_is_not_raw_evidence_equality(self):
        raw = list(parse_map(b"0 1000 1\n"))
        affine = IdMap(raw)
        self.assertNotEqual(raw, affine)
        self.assertEqual(affine, IdMap(raw))
        self.assertEqual([asdict(row) for row in raw], list(asdict(affine)["rows"]))

    def test_only_successful_qualified_observation_delivers_host_coordinates(self):
        raw = consistent_maps(lambda name: b"0 1000 1\n" if name == "uid_map" else b"0 2000 1\n")
        identity = ProcessIdentity(42, 123, FileIdentity(7, 9))
        source = SimpleNamespace(identity=identity, read_maps=lambda: raw)

        @contextmanager
        def succeeds(_pid):
            yield source

        with patch("linux_idmap.process.qualified_process", succeeds):
            observed = observe_host_process(42)
        self.assertIsInstance(observed.uid_map, IdMap)
        self.assertEqual(1000, observed.uid_map.translate(0))
        self.assertEqual(1000, observed.uid_map.translate(0))
        self.assertEqual(2000, observed.gid_map.translate(0))
        self.assertEqual(2000, observed.gid_map.translate(0))
        self.assertEqual(raw.uid_rows, observed.uid_map.rows)

        @contextmanager
        def exit_fails(_pid):
            yield source
            raise IdentityError("process-identity", "source changed before release")

        with patch("linux_idmap.process.qualified_process", exit_fails):
            with self.assertRaises(IdentityError):
                observe_host_process(42)

    def test_identical_printed_rows_do_not_determine_reader_translation(self):
        # Kernel-source model, NOT a real privileged sibling-namespace test.
        # A privileged initial-user parent may legally install target (0, 500, 5),
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
        # Deliberately false affinity assertion: numeric guards cannot detect it.
        self.assertEqual(1002, IdMap(list(parse_map(printed_row(reader_b)))).translate(2))

    def _in_child_user_namespace(self, scenario):
        """Run a scenario in a fresh child USER namespace, never the test runner's."""
        setup = r'''
import ctypes, errno, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from linux_idmap import IdMap, observe_process, parse_map
from linux_idmap._errors import IdentityError
from linux_idmap.process import observe_host_process
parent_uid = os.geteuid()
old_namespace = os.stat('/proc/self/ns/user').st_ino
libc = ctypes.CDLL(None, use_errno=True)
libc.unshare.argtypes = [ctypes.c_int]
libc.unshare.restype = ctypes.c_int
if libc.unshare(0x10000000) != 0:
    error = ctypes.get_errno()
    print(json.dumps({'blocked': 'unshare', 'errno': error, 'reason': os.strerror(error)}))
    raise SystemExit(77 if error in (errno.EPERM, errno.ENOSYS) else 1)
assert os.stat('/proc/self/ns/user').st_ino != old_namespace
'''
        import linux_idmap
        source = str(Path(linux_idmap.__file__).parent.parent)
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", setup + scenario, source],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 77:
            evidence = json.loads(result.stdout)
            unavailable = {
                'unshare': (errno.EPERM, errno.ENOSYS),
                'isolated child uid_map setup': (errno.EPERM, errno.EACCES),
            }
            self.assertIn(evidence['blocked'], unavailable)
            self.assertIn(evidence['errno'], unavailable[evidence['blocked']])
            self.skipTest(f"isolated user namespace setup unavailable: {evidence}")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_isolated_unmapped_reader_preserves_sentinel_without_translation(self):
        evidence = self._in_child_user_namespace(r'''
parent = os.getppid()
assert Path('/proc/self/uid_map').read_bytes() == b''
assert Path('/proc/self/gid_map').read_bytes() == b''
assert list(observe_process(os.getpid()).uid_rows) == []
raw = Path(f'/proc/{parent}/uid_map').read_bytes()
mapping = list(parse_map(raw))
assert mapping
assert all(row.outside == 2**32 - 1 for row in mapping)
assert list(observe_process(parent).uid_rows) == mapping
assert not hasattr(mapping, 'translate')
try:
    observe_host_process(parent)
except IdentityError as error:
    assert error.category == 'host-precondition'
else:
    raise AssertionError('Unmapped reader was host-qualified')
print(json.dumps({'raw': raw.decode(), 'raw_has_translation': hasattr(mapping, 'translate')}))
''')
        self.assertFalse(evidence["raw_has_translation"])

    def test_real_self_map_supports_explicit_nonhost_parent_conversion(self):
        evidence = self._in_child_user_namespace(r'''
try:
    Path('/proc/self/uid_map').write_text(f'0 {parent_uid} 1\n')
except OSError as error:
    print(json.dumps({'blocked': 'isolated child uid_map setup', 'errno': error.errno, 'reason': str(error)}))
    raise SystemExit(77 if error.errno in (errno.EPERM, errno.EACCES) else 1)
assert os.geteuid() == 0
rows = list(observe_process(os.getpid()).uid_rows)
# This source relationship, not the bytes alone, establishes parent affinity:
# reading self's map uses self's immediate parent user namespace coordinates.
parent_map = IdMap(rows)
translated = parent_map.translate(os.geteuid())
assert translated == parent_uid
try:
    observe_host_process(os.getpid())
except IdentityError as error:
    assert error.category == 'host-precondition'
else:
    raise AssertionError('Nested source incorrectly passed host qualification')
print(json.dumps({'raw': Path('/proc/self/uid_map').read_text(),
                  'inside_uid': os.geteuid(), 'recorded_parent_uid': parent_uid,
                  'translated_parent_uid': translated, 'host_qualified': False}))
''')
        self.assertEqual(evidence["recorded_parent_uid"], evidence["translated_parent_uid"])
        self.assertFalse(evidence["host_qualified"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
