"""The complete core runs without add-ons, commands or host qualification."""

from collections.abc import Hashable
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import Mock
import shutil
import subprocess
import sys
import tempfile
import unittest

from linux_idmap import Identifier, IdMap, IdRange, observe_process, parse_map, read_maps


class CoreTests(unittest.TestCase):
    def test_empty_kernel_map_has_no_translation_claim(self):
        mapping = parse_map(b'')
        self.assertIs(iter(mapping), mapping)
        self.assertEqual([], list(mapping))
        self.assertFalse(hasattr(mapping, 'translate'))

    def test_kernel_row_parser_requires_three_integer_columns(self):
        for raw in (b'0 1\n', b'0 1 2 3\n', b'zero 1 2\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                IdRange.of(raw)

    def test_rows_are_lazy_named_immutable_values_in_reported_order(self):
        rows = parse_map(b'  2  1002  3  \n0\t1000\t2\n2 1002\n')
        self.assertIs(iter(rows), rows)
        first = next(rows)
        self.assertEqual(IdRange(2, 1002, 3), first)
        with self.assertRaises(FrozenInstanceError):
            first.outside = 7
        self.assertEqual(IdRange(0, 1000, 2), next(rows))
        with self.assertRaises(ValueError):
            next(rows)

    def test_reader_captures_both_files_before_rows_are_consumed(self):
        read_file = Mock(side_effect=[b'0 1000 1\n', b''])
        maps = read_maps(read_file)
        self.assertEqual(['uid_map', 'gid_map'],
                         [call.args[0] for call in read_file.call_args_list])
        self.assertEqual([IdRange(0, 1000, 1)], list(maps.uid_rows))
        self.assertEqual([], list(maps.gid_rows))

    def test_identifier_validates_without_lossy_coercion(self):
        for value in (0, 1000, 2**32 - 2, Identifier(7)):
            with self.subTest(value=value):
                identifier = Identifier(value)
                self.assertIsInstance(identifier, Identifier)
                self.assertEqual(value, identifier)
        for value in (True, False, 1.0, '1', None, -1, 2**32 - 1, 2**32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Identifier(value)

    def test_explicit_mapping_collects_a_generator_for_repeated_translation(self):
        mapping = IdMap(parse_map(b'0 1000 3\n'))
        self.assertEqual([1002, 1000, 1002, 1001],
                         [mapping.translate(identifier) for identifier in (2, 0, 2, 1)])

    def test_mapping_does_not_keep_the_callers_mutable_row_list(self):
        rows = [IdRange(0, 1000, 1)]
        mapping = IdMap(rows)
        rows[0] = IdRange(0, 2000, 1)
        self.assertEqual(1000, mapping.translate(0))

    def test_owned_rows_support_normal_editing_and_remain_value_comparable(self):
        mapping = IdMap(parse_map(b'0 1000 1\n'))
        mapping.rows += [IdRange(1, 2000, 1)]
        self.assertEqual(1000, mapping.translate(0))
        self.assertEqual(2000, mapping.translate(1))
        self.assertEqual(IdMap([IdRange(0, 1000, 1), IdRange(1, 2000, 1)]), mapping)
        self.assertEqual({'rows': [{'inside': 0, 'outside': 1000, 'length': 1},
                                   {'inside': 1, 'outside': 2000, 'length': 1}]}, asdict(mapping))
        self.assertNotIsInstance(mapping, Hashable)

    def test_identifier_parses_only_decimal_text(self):
        for text in ('0', '0007', '4294967294'):
            self.assertEqual(int(text), Identifier.of(text))
        for text in ('', '+1', '-1', '1_000', ' 1', '1 ', '١', '１',
                     '4294967295', '00000000000', 1, b'1'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Identifier.of(text)

    def test_identifier_checks_the_converted_integer_not_its_subclass(self):
        class ReservedOnConversion(int):
            def __int__(self):
                return 2**32 - 1

        with self.assertRaises(ValueError):
            Identifier(ReservedOnConversion(1))

    def test_text_parsing_uses_the_digits_not_a_numeric_conversion_hook(self):
        class MisleadingText(str):
            def __int__(self):
                return 42

        self.assertEqual(7, Identifier.of(MisleadingText('7')))

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
from linux_idmap import IdMap, IdRange, observe_process
assert IdMap((IdRange(0, 1000, 1),)).translate(0) == 1000
pid = int(sys.argv[2])
observed = observe_process(pid)
for kind in ('uid', 'gid'):
    expected = [tuple(map(int, line.split())) for line in Path(f'/proc/{pid}/{kind}_map').read_bytes().splitlines()]
    actual = [(row.inside, row.outside, row.length) for row in getattr(observed, f'{kind}_rows')]
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
