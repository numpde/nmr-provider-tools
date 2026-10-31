"""Exercise actual checker launch admission without an engine or host effects."""

from tools.container_guard import require_container

require_container()

import os
import pty
import select
import signal
import time
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
            "DOCKER_CONTEXT": "default",
            "DOCKER_CONFIG": "/unreviewed",
            "COMPOSE_FILE": "unreviewed.yml",
            "MAKEFLAGS": "SHELL=malicious .DEFAULT_GOAL=unreviewed",
            "PATH": f"{ROOT / 'tests/repository/fixtures/docker-context'}:{os.environ['PATH']}",
        }
        result = subprocess.run(
            ["make", "--no-print-directory", "tests/repository/command", "DOCKER_CONTEXT=default"],
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
        self.assertIn("unix:///tmp/selected-default.sock", arguments)
        self.assertIn("--pull=never", arguments)
        self.assertIn("--network=none", arguments)
        self.assertNotIn("--privileged", arguments)

    def test_missing_context_without_terminal_refuses_before_engine_use(self):
        environment = os.environ | {"DOCKER_CONTEXT": "unreviewed", "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/poison.sock", "DOCKER_CONTEXT_INPUT_ORIGIN": "command line", "DOCKER_CONTEXT_INPUT": "local-test", "MAKELEVEL": "1"}
        result = subprocess.run(
            ["make", "--no-print-directory", "tests/repository/command"],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, start_new_session=True,
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DOCKER_CONTEXT", result.stderr)
        self.assertNotIn("docker --config", result.stdout)

    def test_selected_context_is_resolved_to_local_socket_and_remote_is_refused(self):
        directory = ROOT / "tests/repository/fixtures/docker-context"
        env = os.environ | {"PATH": f"{directory}:{os.environ['PATH']}", "DOCKER_CONTEXT": "local-test", "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/poison.sock", "MAKELEVEL": "1"}
        command = ["make", "--no-print-directory", "tests/repository/command", "DOCKER_CONTEXT=local-test"]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("unix:///run/user/1000/docker.sock", result.stdout)
        self.assertNotIn("unix:///tmp/poison.sock", result.stdout)
        command[-1] = "DOCKER_CONTEXT=remote-test"
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("ssh://example.invalid", result.stdout)

    def test_interactive_menu_offers_only_local_contexts(self):
        fixture = ROOT / "tests/repository/fixtures/docker-context"
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"DOCKER_CONTEXT_INPUT_ORIGIN", "DOCKER_CONTEXT_INPUT", "NMR_SELECTED_DOCKER_CONTEXT", "NMR_SELECTED_DOCKER_ENDPOINT"}}
        environment["PATH"] = f"{fixture}:{os.environ['PATH']}"
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(ROOT)
            os.execvpe("make", ["make", "--no-print-directory", "tests/repository/command"], environment)
        output = b""
        sent = False
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([terminal], [], [], 0.2)
                if readable:
                    try:
                        part = os.read(terminal, 4096)
                    except OSError:
                        break
                    output += part
                    if not sent and b"Choice [" in output:
                        os.write(terminal, b"1\n")
                        sent = True
            if not sent:
                os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(terminal)
        self.assertTrue(sent, msg=output.decode(errors="replace"))
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, msg=output.decode(errors="replace"))
        self.assertIn(b"local-test", output)
        self.assertNotIn(b"remote-test", output)
        self.assertIn(b"--host unix:///run/user/1000/docker.sock", output)

    def test_multiple_make_goals_resolve_the_context_once(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "context-inspections"
            fixture = ROOT / "tests/repository/fixtures/docker-context"
            environment = os.environ | {"PATH": f"{fixture}:{os.environ['PATH']}", "FAKE_CONTEXT_LOG": str(log)}
            result = subprocess.run(
                ["make", "--no-print-directory", "--dry-run", "tests/repository/command", "checks/image/build", "DOCKER_CONTEXT=local-test"],
                cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(["inspect"], log.read_text().splitlines())

    def test_compound_prepare_reuses_the_original_context_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "tools", root / "tools")
            shutil.copyfile(ROOT / "Makefile", root / "Makefile")
            (root / "pool").mkdir()
            (root / "tools/delivery_cache_check.sh").write_text("printf 'complete\\n'\n")
            (root / "tools/delivery_image.sh").write_text("printf 'selected=%s\\n' \"$NMR_SELECTED_DOCKER_ENDPOINT\"\n")
            log = root / "context-inspections"
            fixture = ROOT / "tests/repository/fixtures/docker-context"
            env = os.environ | {"PATH": f"{fixture}:{os.environ['PATH']}", "FAKE_CONTEXT_LOG": str(log)}
            result = subprocess.run(
                ["make", "--no-print-directory", "delivery/prepare", f"WHEELHOUSE={root / 'pool'}", "DOCKER_CONTEXT=local-test"],
                cwd=root, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertIn("selected=unix:///run/user/1000/docker.sock", result.stdout)
            self.assertEqual(["inspect"], log.read_text().splitlines())

    def test_preview_discovers_new_public_roots_and_rejects_hidden_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("tools", "requirements", "containers"):
                shutil.copytree(ROOT / name, root / name)
            (root / "future-source").mkdir()
            (root / ".work").mkdir()
            (root / ".aws").mkdir()
            launcher = root / "tools/repository.sh"
            launcher.write_bytes((ROOT / "tools/repository.sh").read_bytes())
            environment = os.environ | {"NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock"}
            command = ["bash", str(launcher), "--print", "tests/repository"]
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertIn(f"type=bind,src={root}/future-source,dst=/workspace/future-source,readonly", arguments)
            self.assertNotIn(f"type=bind,src={root}/.aws,dst=/workspace/.aws,readonly", arguments)
            self.assertIn(f"type=bind,src={root}/.work,dst=/workspace/.work,readonly", arguments)
            (root / "linked").symlink_to(root / "future-source", target_is_directory=True)
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
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
