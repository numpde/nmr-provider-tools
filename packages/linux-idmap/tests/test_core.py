"""The complete core runs without add-ons, commands or host qualification."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from linux_idmap import observe_process, parse_map


class CoreTests(unittest.TestCase):
    def test_empty_kernel_map_and_unmapped_ids(self):
        mapping = parse_map(b'')
        self.assertEqual((), mapping.rows)
        for identifier in (0, -1, True, 1.0, '1', 2**32 - 1):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                mapping.translate(identifier)

    def test_kernel_row_parser_requires_three_integer_columns(self):
        for raw in (b'0 1\n', b'0 1 2 3\n', b'zero 1 2\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_map(raw)

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
    actual = [(row.inside, row.outside, row.length) for row in getattr(observed, name).rows]
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
