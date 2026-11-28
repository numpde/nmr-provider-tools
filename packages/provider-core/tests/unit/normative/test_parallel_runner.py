"""Module workers preserve fixtures, failure status and asyncio diagnostics."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import unittest


class ParallelRunnerTests(unittest.TestCase):
    def run_fixture(self, source, workers="2", *, interrupt=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            unit = tests / "unit"
            unit.mkdir(parents=True)
            (tests / "__init__.py").touch()
            (unit / "__init__.py").touch()
            shutil.copyfile(Path(__file__).resolve().parents[2] / "run_suite.py", tests / "run_suite.py")
            (unit / "test_fixture.py").write_text(source)
            command = [sys.executable, "-I", "-B", str(tests / "run_suite.py"), "unit", "test*.py"]
            environment = os.environ | {"PROVIDER_CORE_TEST_WORKERS": workers}
            if interrupt:
                process = subprocess.Popen(command, cwd=root, env=environment,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not (root / "worker.pid").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((root / "worker.pid").exists())
                    worker_pid = int((root / "worker.pid").read_text())
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                with self.assertRaises(ProcessLookupError):
                    os.kill(worker_pid, 0)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            return subprocess.run(
                [sys.executable, "-I", "-B", str(tests / "run_suite.py"), "unit", "test*.py"],
                env=os.environ | {"PROVIDER_CORE_TEST_WORKERS": workers},
                capture_output=True, text=True, timeout=30,
            )

    def test_parent_termination_joins_worker_process(self):
        result = self.run_fixture('''import unittest, os, time
from pathlib import Path
class Cases(unittest.TestCase):
    def test_wait(self):
        Path("worker.pid").write_text(str(os.getpid()))
        time.sleep(60)
''', interrupt=True)
        self.assertNotEqual(result.returncode, 0)

    def test_module_fixtures_and_all_cases_run_once(self):
        result = self.run_fixture('''import unittest
def setUpModule():
    print("MODULE_SETUP_WITNESS")
def tearDownModule():
    print("MODULE_TEARDOWN_WITNESS")
class Cases(unittest.TestCase):
    def test_first(self): pass
    def test_second(self): pass
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("MODULE_SETUP_WITNESS"), 1)
        self.assertEqual(result.stdout.count("MODULE_TEARDOWN_WITNESS"), 1)
        self.assertIn("Ran 2 tests", result.stdout)
        self.assertIn("MODULE_RESULT tests.unit.test_fixture status=0", result.stdout)
        self.assertIn("SUITE_TIME unit mode=parallel workers=2 seconds=", result.stdout)
        self.assertIn("SUITE_TIME unit mode=serial seconds=", result.stdout)

    def test_worker_failure_and_asyncio_error_keep_nonzero_status(self):
        for statement, evidence in (
            ('self.fail("intentional failure")', "intentional failure"),
            ('logging.getLogger("asyncio").error("unowned worker error")', "Unowned asyncio ERROR"),
        ):
            with self.subTest(statement=statement):
                result = self.run_fixture(
                    "import unittest, logging\nclass Cases(unittest.TestCase):\n"
                    f"    def test_failure(self): {statement}\n"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(evidence, result.stdout)
                self.assertIn("FAIL tests.unit.test_fixture", result.stderr)

    def test_empty_discovery_and_invalid_worker_count_fail(self):
        valid = "import unittest\nclass Cases(unittest.TestCase):\n    def test_ok(self): pass\n"
        for source, workers in (("", "2"), (valid, "0")):
            with self.subTest(workers=workers):
                result = self.run_fixture(source, workers)
                self.assertNotEqual(result.returncode, 0)
