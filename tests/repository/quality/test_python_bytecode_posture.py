"""Literal first-party launches suppress bytecode before their Python payload."""
from tools.container_guard import require_container
require_container()

from pathlib import Path
import tempfile
import unittest

from .python_bytecode import findings, production_files, scan
from .sources import ROOT


class PythonBytecodePostureTests(unittest.TestCase):
    def test_literal_shell_and_make_launches_respect_payload_position(self):
        for source in ("python3 -I -S identity.py", "python3 -E identity.py",
                       "python3 identity.py -B", "python3 -m tools.helper -B",
                       "result=$(python3 -I helper.py)",
                       "native 'mapping' untimed stdio python3 -I -S identity.py",
                       "PYTHONDONTWRITEBYTECODE=1 python3 -I identity.py",
                       "export PYTHONDONTWRITEBYTECODE=1\npython3 -E identity.py"):
            with self.subTest(source=source):
                self.assertEqual(1, len(findings(Path("launcher.sh"), source)))
        self.assertEqual(1, len(findings(Path("Makefile"), "run:\n\t@python3 -I helper.py\n")))

    def test_effective_literal_environment_and_flags_are_accepted(self):
        for source in ("python3 -I -S -B identity.py", "python3 -IBS identity.py",
                       "PYTHONDONTWRITEBYTECODE=1 python3 helper.py",
                       "env -u PYTHONDONTWRITEBYTECODE PYTHONDONTWRITEBYTECODE=1 python3 helper.py",
                       "export PYTHONDONTWRITEBYTECODE=1\npython3 helper.py",
                       "docker run --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python3 image helper.py"):
            with self.subTest(source=source):
                self.assertEqual([], findings(Path("launcher.sh"), source))
        self.assertEqual(1, len(findings(Path("launcher.sh"),
            "env PYTHONDONTWRITEBYTECODE=1 -u PYTHONDONTWRITEBYTECODE python3 helper.py")))
        self.assertEqual(1, len(findings(Path("launcher.sh"),
            "PYTHONDONTWRITEBYTECODE=1 docker run --entrypoint python3 image helper.py")))

    def test_entrypoint_declarations_shared_arrays_and_compilers_are_explicit_boundaries(self):
        for source in ("options=(--entrypoint python3)",
                       'checker_host_identity "$image" /usr/local/bin/python3 "$uid" "$gid"',
                       'run "${worker[@]}" --entrypoint python3 "$image" -m tools.helper',
                       "python3 -I -m pip install dependency", "python3 -I -m venv /runtime",
                       "python3 -I -c 'import sys; print(sys.version)'",
                       '"$python" "${arguments[@]}"', 'python3 "${arguments[@]}"'):
            with self.subTest(source=source):
                self.assertEqual([], findings(Path("launcher.sh"), source))
        self.assertEqual(1, len(findings(Path("launcher.sh"),
            'run "${worker[@]}" --entrypoint python3 "$image" -I -S identity.py')))

    def test_python_literal_subprocess_lists_and_environment_overrides(self):
        bad = [
            'subprocess.run(["python3", "-I", "identity.py"])',
            'subprocess.run([sys.executable, "-E", "identity.py"], env={"PYTHONDONTWRITEBYTECODE":"1"})',
            'subprocess.run(["python3", "helper.py", "-B"])',
            'subprocess.run(["python3", "helper.py"], env={"PYTHONDONTWRITEBYTECODE":"1", **other})',
        ]
        good = [
            'subprocess.run([sys.executable, "-I", "-B", "identity.py"])',
            'subprocess.run(["python3", "helper.py"], env={"PYTHONDONTWRITEBYTECODE":"1"})',
            'subprocess.run(["python3", "helper.py"], env=os.environ | {"PYTHONDONTWRITEBYTECODE":"1"})',
            'subprocess.run([sys.executable, "-I", "-m", "pip", "install", "wheel"])',
            'subprocess.run([executable, *arguments])',
        ]
        for source in bad:
            self.assertEqual(1, len(findings(Path("driver.py"), source)), msg=source)
        for source in good:
            self.assertEqual([], findings(Path("driver.py"), source), msg=source)

    def test_inventory_is_automatic_and_diagnostics_do_not_echo_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("host/tools/run.sh", "new_component/driver.py", "Makefile", "tests/fixtures/run.sh",
                         "bootstrap/generated/run.sh", "vendor/run.py", "docs/example.sh", ".git/hooks/run.sh"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# fixture\n")
            self.assertEqual({"host/tools/run.sh", "new_component/driver.py", "Makefile"},
                             {path.relative_to(root).as_posix() for path in production_files(root)})
        issue, = findings(Path("safe.sh"), "python3 -I secret-private-path.py")
        self.assertNotIn("secret-private-path", issue.diagnostic())
        self.assertIn("safe.sh:1:", issue.diagnostic())

    def test_production_literal_python_launches_suppress_bytecode(self):
        issues = scan(ROOT)
        self.assertEqual([], [issue.diagnostic() for issue in issues])
