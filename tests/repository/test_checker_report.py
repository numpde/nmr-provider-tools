"""Exercise report discovery, failure accounting and the stdout artifact boundary."""

from tools.container_guard import require_container

require_container()

from collections import Counter
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


class CheckerReportTests(unittest.TestCase):
    def test_discovered_outcomes_produce_html_without_hiding_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = pathlib.Path(directory) / "test_report_fixture.py"

            source_path.write_text(
                textwrap.dedent(
                    '''
                import unittest
                from operations.checker.findings import assert_no_findings

                class Outcomes(unittest.TestCase):
                    def test_success(self):
                        assert_no_findings([])

                    def test_failure(self):
                        self.fail("fixture rejection")

                    def test_inventory(self):
                        assert_no_findings(["first finding", "second finding", "third finding"])

                    def test_error(self):
                        raise RuntimeError("fixture error")

                    @unittest.skip("fixture skip")
                    def test_skipped(self):
                        pass

                    @unittest.expectedFailure
                    def test_expected_failure(self):
                        self.fail("expected fixture rejection")

                    @unittest.expectedFailure
                    def test_unexpected_success(self):
                        pass

                    def test_subtests(self):
                        for number in [0, 1]:
                            with self.subTest(number=number):
                                self.assertEqual(0, number)

                        for finding in ["fourth finding", "fifth finding"]:
                            with self.subTest(finding=finding):
                                assert_no_findings([finding])

                class BrokenSetup(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        raise RuntimeError("fixture setup failure")

                    def test_unreachable(self):
                        pass
                    '''
                ),
            )

            environment = os.environ | {
                "CHECK_REPORT_REVISION": "fixture-revision",
                "CHECK_REPORT_CONTEXT": "fixture</script>context",
                "CHECK_REPORT_PATTERN": "test*.py",
                "PRIVACY_TESTS": "0",
            }

            completed_checks = subprocess.run(
                [sys.executable, "-B", "-m", "operations.checker.report", "discover", "-s", directory, "-v"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(1, completed_checks.returncode)
        self.assertTrue(completed_checks.stdout.startswith("<!doctype html>"))
        self.assertIn("fixture setup failure", completed_checks.stderr)
        report_json = completed_checks.stdout.split('<script id="results" type="application/json">', 1)[1].split("</script>", 1)[0]
        report = json.loads(report_json)

        self.assertEqual(
            {"passed": 1, "failed": 3, "error": 2, "skipped": 1, "expected failure": 1, "unexpected success": 1},
            Counter(check["outcome"] for check in report["checks"].values()),
        )

        self.assertEqual("fixture-revision", report["revision"])
        self.assertEqual("fixture</script>context", report["context"])
        self.assertFalse(report["privacyTests"])
        self.assertEqual(3, report["checks"]["test_report_fixture.Outcomes.test_inventory"]["findings"])
        self.assertEqual(2, report["checks"]["test_report_fixture.Outcomes.test_subtests"]["findings"])
        self.assertFalse(any(("test_unreachable" in check_id) for check_id in report["checks"]))
