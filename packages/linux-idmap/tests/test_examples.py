"""Execute the canonical caller programs against the active package installation."""

import os
from pathlib import Path
import subprocess
import sys
import unittest

import linux_idmap


class ExampleTests(unittest.TestCase):
    def run_example(self, name, *arguments):
        script = Path(__file__).resolve().parents[1] / 'examples' / name
        package_root = Path(linux_idmap.__file__).resolve().parent.parent
        return subprocess.run(
            [sys.executable, '-S', '-B', str(script), *arguments],
            env={**os.environ, 'PYTHONPATH': str(package_root)},
            capture_output=True, text=True, timeout=5,
        )

    def test_read_self_prints_the_inherited_namespace_rows(self):
        result = self.run_example('read_self.py')
        expected = [f'{kind} {" ".join(line.split())}'
                    for kind in ('uid', 'gid')
                    for line in Path(f'/proc/self/{kind}_map').read_text().splitlines()]
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected, result.stdout.splitlines())
        self.assertEqual('', result.stderr)

    def test_affine_translation_prints_1001(self):
        result = self.run_example('translate.py')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('1001\n', result.stdout)
        self.assertEqual('', result.stderr)

    def test_host_ids_reports_ids_or_the_actual_namespace_refusal(self):
        result = self.run_example('host_ids.py')
        if os.stat('/proc/self/ns/user').st_ino == 4026531837:
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(f'{os.getuid()} {os.getgid()}\n', result.stdout)
            self.assertEqual('', result.stderr)
        else:
            self.assertNotEqual(0, result.returncode)
            self.assertEqual('', result.stdout)
            self.assertEqual('host-precondition: Map reader must be in the initial user namespace\n',
                             result.stderr)

    def test_probe_rejects_missing_arguments_and_invalid_uid_before_dispatch(self):
        for arguments, message in (
            ((), 'usage: probe_image.py ENDPOINT IMAGE PYTHON_PATH UID GID\n'),
            (('unix:///run/docker.sock', 'my-image:local', '/usr/bin/python3', 'invalid', '1000'),
             'expected a decimal Linux ID\n'),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_example('probe_image.py', *arguments)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual('', result.stdout)
                self.assertEqual(message, result.stderr)
