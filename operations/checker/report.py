"""Render discovered unittest outcomes as a self-contained HTML report.

Normal unittest diagnostics stay on stderr. Only the report goes to stdout,
so the host can redirect it without granting the checker source writes.
"""

from tools.container_guard import require_container

require_container()

import datetime
import json
import os
import pathlib
import unittest

from operations.checker.findings import FindingsFailure


class ReportResult(unittest.TextTestResult):
    """Retain outcomes alongside unittest's ordinary diagnostics and exit status."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checks = {}

    def startTest(self, test):
        super().startTest(test)
        self.checks[test.id()] = {"outcome": "passed"}

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.checks[test.id()] = {"outcome": "failed"}

        if isinstance(err[1], FindingsFailure):
            self.checks[test.id()]["findings"] = len(err[1].findings)

    def addError(self, test, err):
        super().addError(test, err)
        self.checks[test.id()] = {"outcome": "error"}

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.checks[test.id()] = {"outcome": "skipped"}

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.checks[test.id()] = {"outcome": "expected failure"}

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.checks[test.id()] = {"outcome": "unexpected success"}

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)

        if err is not None:
            # A failing subtest makes its parent check red without counting it twice
            if self.checks[test.id()]["outcome"] != "error":
                if issubclass(err[0], test.failureException):
                    self.checks[test.id()]["outcome"] = "failed"
                else:
                    self.checks[test.id()]["outcome"] = "error"

            if isinstance(err[1], FindingsFailure):
                parent = self.checks[test.id()]

                if "findings" in parent:
                    parent["findings"] += len(err[1].findings)
                else:
                    parent["findings"] = len(err[1].findings)


class ReportRunner(unittest.TextTestRunner):
    """Emit the finished report even when discovered checks fail."""

    resultclass = ReportResult

    def run(self, test):
        test_result = super().run(test)

        report = {
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "revision": os.environ["CHECK_REPORT_REVISION"],
            "context": os.environ["CHECK_REPORT_CONTEXT"],
            "pattern": os.environ["CHECK_REPORT_PATTERN"],
            "privacyTests": os.environ["PRIVACY_TESTS"] == "1",
            "checks": test_result.checks,
        }

        template = pathlib.Path(__file__).with_suffix(".html").read_text()

        if "CHECK_SOURCE_ROOT" in os.environ:
            report["sourceRoot"] = os.environ["CHECK_SOURCE_HOST_ROOT"]
            report["sourceFiles"] = os.environ["CHECK_SOURCE_FILES"].split()

        # A test identifier must not close the embedded JSON script element
        report_json = json.dumps(report, ensure_ascii=True).replace("<", "\\u003c")

        print(template.replace("REPORT_JSON", report_json), end="")

        return test_result


if __name__ == "__main__":
    unittest.main(module=None, testRunner=ReportRunner)
