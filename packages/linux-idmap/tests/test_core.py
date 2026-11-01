"""The complete core runs without add-ons, commands or host qualification."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from linux_idmap import IdRange, Maps, observe_process, parse_map, read_maps


class CoreTests(unittest.TestCase):
    def test_empty_kernel_map_has_no_translation_claim(self):
        mapping = parse_map(b'')
        self.assertEqual((), mapping)
        self.assertFalse(hasattr(mapping, 'translate'))

    def test_kernel_row_parser_requires_three_integer_columns(self):
        for raw in (b'0 1\n', b'0 1 2 3\n', b'zero 1 2\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_map(raw)

    def test_padded_rows_are_eager_named_immutable_snapshots(self):
        rows = parse_map(b'  2  1002  3  \n0\t1000\t2\n')
        self.assertIsInstance(rows, tuple)
        self.assertEqual((IdRange(2, 1002, 3), IdRange(0, 1000, 2)), rows)
        with self.assertRaises(FrozenInstanceError):
            rows[0].outside = 7
        self.assertNotEqual(rows, tuple(reversed(rows)))
        self.assertNotEqual(rows, parse_map(b'0 1000 5\n'))
        # A later malformed row must fail now, not during iteration by a caller.
        with self.assertRaises(ValueError):
            parse_map(b'0 1000 2\n2 1002\n')

    def test_reader_returns_two_raw_row_snapshots_in_file_order(self):
        calls = []
        def read_file(name):
            calls.append(name)
            return {'uid_map': b'0 1000 1\n', 'gid_map': b''}[name]
        self.assertEqual(Maps((IdRange(0, 1000, 1),), ()), read_maps(read_file))
        self.assertEqual(['uid_map', 'gid_map'], calls)

    def test_pid_is_an_explicit_positive_integer(self):
        for pid in (0, -1, True, 1.0, '../self'):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                observe_process(pid)

    def test_two_file_core_reads_real_tokenless_child_without_addons_or_path(self):
        import linux_idmap
        source = Path(linux_idmap.__file__).parent
        with tempfile.TemporaryDirectory(prefix='linux-idmap-core-') as directory:
            package = Path(directory) / 'linux_idmap'
            package.mkdir()
            for name in ('__init__.py', 'core.py'):
                shutil.copyfile(source / name, package / name)
            program = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from linux_idmap import observe_process
pid = int(sys.argv[2])
observed = observe_process(pid)
for name in ('uid_map', 'gid_map'):
    expected = [tuple(map(int, line.split())) for line in Path(f'/proc/{pid}/{name}').read_bytes().splitlines()]
    actual = [(row.inside, row.outside, row.length) for row in getattr(observed, name)]
    assert actual == expected
assert not any(name.startswith('linux_idmap.addons') for name in sys.modules)
'''
            child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'import time; time.sleep(30)'])
            try:
                result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', program, directory, str(child.pid)],
                                        env={'PATH': ''}, capture_output=True, text=True, timeout=5)
                self.assertEqual(0, result.returncode, result.stderr)
            finally:
                child.terminate()
                child.wait(timeout=3)
