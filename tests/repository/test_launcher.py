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
import pathlib

from tests.repository.quality.sources import ROOT


class LauncherTests(unittest.TestCase):
    def test_quality_filter_selects_a_test_and_rejects_invalid_selection(self):
        environment = os.environ | {"NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/test.sock"}
        command = ["bash", "tests/repository/run.sh", "--print", "tests/repository/quality"]

        for (origin, pattern, expected) in (
            ("undefined", "test_statement_spacing_posture.py", "test*.py"),
            ("command line", "test_statement_spacing_posture.py", "test_statement_spacing_posture.py"),
            ("command line", "test_python_empty_defaults.py", "test_python_empty_defaults.py"),
        ):
            with self.subTest(origin=origin, pattern=pattern):
                environment.update(TEST_PATTERN_INPUT_ORIGIN=origin, TEST_PATTERN_INPUT=pattern)
                completed_process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)

                self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
                arguments = shlex.split(completed_process.stdout)

                self.assertEqual(expected, arguments[arguments.index("-p") + 1])

        for pattern in ("test_missing_quality_file.py", "../test_escape.py", "test_*.py", ""):
            with self.subTest(pattern=pattern):
                environment.update(TEST_PATTERN_INPUT_ORIGIN="command line", TEST_PATTERN_INPUT=pattern)
                completed_process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)

                self.assertNotEqual(0, completed_process.returncode)
                self.assertIn("Cannot select repository test", completed_process.stderr)
                self.assertEqual("", completed_process.stdout)

        environment.update(TEST_PATTERN_INPUT_ORIGIN="command line", TEST_PATTERN_INPUT="test_makefile.py")
        command[-1] = "tests/repository/runtime"
        completed_process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)

        self.assertNotEqual(0, completed_process.returncode)
        self.assertIn("requires tests/repository or tests/repository/quality", completed_process.stderr)

    def test_preview_ignores_ambient_engine_and_make_overrides(self):
        environment = os.environ | {
            "DOCKER_HOST": "tcp://unreviewed.invalid:2375",
            "DOCKER_CONTEXT": "default",
            "DOCKER_CONFIG": "/unreviewed",
            "COMPOSE_FILE": "unreviewed.yml",
            "MAKEFLAGS": "SHELL=malicious .DEFAULT_GOAL=unreviewed",
            "PATH": f"{ROOT / 'tests/repository/fixtures/docker-context'}:{os.environ['PATH']}",
        }

        completed_process = subprocess.run(
            ["make", "--no-print-directory", "tests/repository/command", "DOCKER_CONTEXT=default"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
        arguments = shlex.split(completed_process.stdout.splitlines()[-1])

        self.assertNotIn("unreviewed", " ".join(arguments))
        self.assertEqual("env", arguments[0])
        self.assertIn("DOCKER_HOST", arguments)
        self.assertIn("unix:///tmp/selected-default.sock", arguments)
        self.assertIn("--pull=never", arguments)
        self.assertIn("--network=none", arguments)
        self.assertNotIn("--privileged", arguments)

    def test_missing_context_without_terminal_refuses_before_engine_use(self):
        environment = os.environ | {"DOCKER_CONTEXT": "unreviewed", "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/poison.sock", "DOCKER_CONTEXT_INPUT_ORIGIN": "command line", "DOCKER_CONTEXT_INPUT": "local-test", "MAKELEVEL": "1"}

        completed_process = subprocess.run(
            ["make", "--no-print-directory", "tests/repository/command"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(0, completed_process.returncode)
        self.assertIn("DOCKER_CONTEXT", completed_process.stderr)
        self.assertNotIn("docker --config", completed_process.stdout)

    def test_selected_context_is_resolved_to_local_socket_and_remote_is_refused(self):
        directory = ROOT / "tests/repository/fixtures/docker-context"
        env = os.environ | {"PATH": f"{directory}:{os.environ['PATH']}", "DOCKER_CONTEXT": "local-test", "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/poison.sock", "MAKELEVEL": "1"}
        command = ["make", "--no-print-directory", "tests/repository/command", "DOCKER_CONTEXT=local-test"]
        completed_process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

        self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
        self.assertIn("unix:///run/user/1000/docker.sock", completed_process.stdout)
        self.assertNotIn("unix:///tmp/poison.sock", completed_process.stdout)
        command[-1] = "DOCKER_CONTEXT=remote-test"
        completed_process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

        self.assertNotEqual(0, completed_process.returncode)
        self.assertNotIn("ssh://example.invalid", completed_process.stdout)

    def test_interactive_menu_offers_only_local_contexts(self):
        fixture = ROOT / "tests/repository/fixtures/docker-context"

        environment = {key: value for (key, value) in os.environ.items()
                       if key not in {"DOCKER_CONTEXT_INPUT_ORIGIN", "DOCKER_CONTEXT_INPUT", "NMR_SELECTED_DOCKER_CONTEXT", "NMR_SELECTED_DOCKER_ENDPOINT"}}

        environment["PATH"] = f"{fixture}:{os.environ['PATH']}"
        (pid, terminal) = pty.fork()

        if pid == 0:
            os.chdir(ROOT)
            os.execvpe("make", ["make", "--no-print-directory", "tests/repository/command"], environment)

        output = b""
        sent = False
        deadline = time.monotonic() + 10

        try:
            while time.monotonic() < deadline:
                (readable, _, _) = select.select([terminal], [], [], 0.2)

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

            (_, status) = os.waitpid(pid, 0)
        finally:
            os.close(terminal)

        self.assertTrue(sent, msg=output.decode(errors="replace"))
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, msg=output.decode(errors="replace"))
        self.assertIn(b"local-test", output)
        self.assertNotIn(b"remote-test", output)
        self.assertIn(b"--host unix:///run/user/1000/docker.sock", output)

    def test_multiple_make_goals_resolve_the_context_once(self):
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "context-inspections"
            fixture = ROOT / "tests/repository/fixtures/docker-context"
            environment = os.environ | {"PATH": f"{fixture}:{os.environ['PATH']}", "FAKE_CONTEXT_LOG": str(log)}

            completed_process = subprocess.run(
                ["make", "--no-print-directory", "tests/repository/command", "help", "DOCKER_CONTEXT=local-test"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
            self.assertEqual(["inspect"], log.read_text().splitlines())

    def test_make_queries_do_not_select_resources_with_a_terminal(self):
        fixture = ROOT / "tests/repository/fixtures/docker-context"

        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "context-inspections"
            environment = {key: value for (key, value) in os.environ.items()
                           if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "GNUMAKEFLAGS", "MAKELEVEL"}}
            environment.update(PATH=f"{fixture}:{os.environ['PATH']}", FAKE_CONTEXT_LOG=str(log))

            for options in (("-rR", "-qp"), ("--question",), ("--dry-run",)):
                with self.subTest(options=options):
                    (pid, terminal) = pty.fork()

                    if pid == 0:
                        os.chdir(ROOT)
                        os.execvpe("make", ["make", "--no-print-directory", *options, "checks/prepare"], environment)

                    output = b""
                    status = None
                    deadline = time.monotonic() + 10
                    terminal_closed = False

                    try:
                        while time.monotonic() < deadline:
                            if not terminal_closed and select.select([terminal], [], [], 0.1)[0]:
                                try:
                                    output += os.read(terminal, 65536)
                                except OSError:
                                    terminal_closed = True

                            (finished, value) = os.waitpid(pid, os.WNOHANG)

                            if finished:
                                status = value
                                break

                        if status is None:
                            os.killpg(pid, signal.SIGKILL)
                            os.waitpid(pid, 0)
                    finally:
                        os.close(terminal)

                    self.assertIsNotNone(status, "Make query blocked on operator input")
                    self.assertTrue(os.WIFEXITED(status), msg=output[-2000:].decode(errors="replace"))
                    self.assertIn(os.WEXITSTATUS(status), (0, 1), msg=output[-2000:].decode(errors="replace"))
                    self.assertNotIn(b"Select a local Docker context", output)
                    self.assertNotIn(b"WHEELHOUSE (", output)
                    self.assertNotIn(b"Select NETWORK_INTERFACE", output)
                    self.assertFalse(log.exists(), "Make query inspected Docker")

    def test_preview_discovers_new_public_roots_and_rejects_hidden_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            for name in ("tools", "host", "requirements", "operations"):
                shutil.copytree(ROOT / name, root / name)

            (root / "future-source").mkdir()
            (root / ".work").mkdir()
            (root / ".aws").mkdir()
            (root / "release-artifacts").mkdir()
            launcher = root / "tests/repository/run.sh"
            launcher.parent.mkdir(parents=True)

            launcher.write_bytes((ROOT / "tests/repository/run.sh").read_bytes())
            environment = os.environ | {"NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock"}
            command = ["bash", str(launcher), "--print", "tests/repository"]
            completed_process = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

            self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
            arguments = shlex.split(completed_process.stdout)

            self.assertIn(f"type=bind,src={root}/future-source,dst=/workspace/future-source,readonly", arguments)
            self.assertNotIn(f"type=bind,src={root}/.aws,dst=/workspace/.aws,readonly", arguments)
            self.assertNotIn(f"type=bind,src={root}/release-artifacts,dst=/workspace/release-artifacts,readonly", arguments)
            self.assertIn(f"type=bind,src={root}/.work,dst=/workspace/.work,readonly", arguments)
            (root / "linked").symlink_to(root / "future-source", target_is_directory=True)
            completed_process = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

            self.assertNotEqual(0, completed_process.returncode)
            self.assertEqual("", completed_process.stdout)
            self.assertIn("Cannot grant repository source", completed_process.stderr)

    def test_missing_inputs_and_unknown_target_fail_before_engine_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            launcher = root / "tests/repository/run.sh"
            launcher.parent.mkdir(parents=True)

            launcher.write_bytes((ROOT / "tests/repository/run.sh").read_bytes())

            for (target, message) in (
                ("tests/repository", "operations/checker/image_identity.sh"),
                ("unreviewed", "unknown target"),
            ):
                completed_process = subprocess.run(
                    ["bash", str(launcher), "--print", target],
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(0, completed_process.returncode)
                self.assertEqual("", completed_process.stdout)
                self.assertIn(message, completed_process.stderr)

    def test_empty_unittest_discovery_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            completed_process = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", directory, "-v"],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, completed_process.returncode)
            self.assertIn("NO TESTS RAN", completed_process.stderr)
