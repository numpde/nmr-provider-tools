"""Exercise actual checker launch admission without an engine or host effects."""

from tools.container_guard import require_container

require_container()

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.repository.quality.sources import ROOT


class LauncherTests(unittest.TestCase):
    def test_preview_ignores_ambient_engine_and_make_overrides(self):
        environment = os.environ | {
            "DOCKER_HOST": "tcp://unreviewed.invalid:2375",
            "DOCKER_CONTEXT": "unreviewed",
            "DOCKER_CONFIG": "/unreviewed",
            "COMPOSE_FILE": "unreviewed.yml",
            "MAKEFLAGS": "SHELL=malicious .DEFAULT_GOAL=unreviewed",
        }
        result = subprocess.run(
            ["make", "--no-print-directory", "tests/repository/command"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout.splitlines()[-1])
        self.assertNotIn("unreviewed", " ".join(arguments))
        self.assertEqual("env", arguments[0])
        self.assertIn("DOCKER_HOST", arguments)
        self.assertIn("unix:///var/run/docker.sock", arguments)
        self.assertIn("--pull=never", arguments)
        self.assertIn("--network=none", arguments)
        self.assertNotIn("--privileged", arguments)

    def test_preview_discovers_new_public_roots_and_rejects_hidden_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("tools", "requirements", "containers"):
                shutil.copytree(ROOT / name, root / name)
            (root / "future-source").mkdir()
            (root / ".aws").mkdir()
            launcher = root / "tools/repository.sh"
            launcher.write_bytes((ROOT / "tools/repository.sh").read_bytes())
            command = ["bash", str(launcher), "--print", "tests/repository"]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertIn(f"type=bind,src={root}/future-source,dst=/workspace/future-source,readonly", arguments)
            self.assertNotIn(f"type=bind,src={root}/.aws,dst=/workspace/.aws,readonly", arguments)
            self.assertFalse((root / ".work").exists())
            (root / "linked").symlink_to(root / "future-source", target_is_directory=True)
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("Cannot grant repository source", result.stderr)

    def test_missing_inputs_and_unknown_target_fail_before_engine_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            launcher = root / "tools/repository.sh"
            launcher.write_bytes((ROOT / "tools/repository.sh").read_bytes())
            for target, message in (
                ("tests/repository", "checker_inputs.sh"),
                ("unreviewed", "unknown target"),
            ):
                result = subprocess.run(
                    ["bash", str(launcher), "--print", target],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn(message, result.stderr)

    def test_empty_unittest_discovery_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", directory, "-v"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("NO TESTS RAN", result.stderr)
