"""Run only installed core tests; an empty selection is a qualification failure."""

from provider_core.container_guard import require_container

require_container()

import importlib.metadata
import logging
import sys
import traceback
import unittest
from pathlib import Path

import provider_core


# The installed runner is executed as a script (__main__), while its own
# regression tests import tests.run_suite. Both names must identify the same
# module or two independent observer stacks will count nested test errors twice.
if __name__ == "__main__":
    sys.modules["tests.run_suite"] = sys.modules[__name__]


# Observe the asyncio logger for the complete unittest runner lifetime, not
# merely between startTest and stopTest. IsolatedAsyncioTestCase closes its loop
# AFTER stopTest, and errors during that shutdown must still fail the test.
# Nested runners take precedence so the outer suite never double-counts a fault.
_ACTIVE_ASYNCIO_OBSERVERS = []


class _AsyncioDiagnosticError(RuntimeError):
    """An unowned asyncio ERROR has the same failed-run status as a test error."""


class _UnattributedAsyncioTest:
    def __str__(self):
        return "asyncio diagnostic outside any TestCase"

    def shortDescription(self):
        return None


class _AsyncioDiagnosticObserver(logging.Handler):
    def __init__(self, result):
        super().__init__(level=logging.ERROR)
        self.result = result

    def emit(self, record):
        if not _ACTIVE_ASYNCIO_OBSERVERS or _ACTIVE_ASYNCIO_OBSERVERS[-1] is not self:
            return
        cause = None
        if record.exc_info:
            cause = record.exc_info[1]
        kind = "No exception object"
        if cause is not None:
            kind = type(cause).__name__
        original_context = record.getMessage()
        message = "(no asyncio message)"
        message_lines = original_context.splitlines()
        if message_lines:
            message = message_lines[0]
        details = []
        if cause is not None:
            details = str(cause).splitlines()
        detail = ""
        if details:
            detail = ": " + details[0][:160]
        diagnostic = _AsyncioDiagnosticError(
            f"Unowned asyncio ERROR: {message[:180]} ({kind}{detail}); "
            f"origin {record.pathname}:{record.lineno}"
        )
        if cause is not None:
            diagnostic.__cause__ = cause
        if "\n" in original_context:
            # Preserve the original handle_traceback, protocol, and transport
            # details in unittest's full error report, not the short index.
            context_limit = 8192
            context = original_context[:context_limit]
            if len(original_context) > context_limit:
                context += "\n... further asyncio context truncated"
            diagnostic.add_note("Original asyncio event context:\n" + context)
        case = self.result._active_test
        if case is None:
            case = self.result._unattributed_asyncio_test
        try:
            raise diagnostic
        except _AsyncioDiagnosticError:
            self.result.addError(case, sys.exc_info())
            self.result._asyncio_errored.add(case)


# The complete unittest tracebacks remain above the final, bounded failure
# index. Capture the exception at the original result callback so subtest
# parameters and primary error types survive formatting and exception chaining.
class _FailureSummaryResult(unittest.TextTestResult):
    def __init__(self, *arguments, **settings):
        super().__init__(*arguments, **settings)
        self.problems = []
        self._active_test = None
        self._asyncio_errored = set()
        self._unattributed_asyncio_test = _UnattributedAsyncioTest()

    def startTestRun(self):
        super().startTestRun()
        self._asyncio_logger = logging.getLogger("asyncio")
        self._asyncio_observer = _AsyncioDiagnosticObserver(self)
        self._asyncio_logger.addHandler(self._asyncio_observer)
        _ACTIVE_ASYNCIO_OBSERVERS.append(self._asyncio_observer)

    def stopTestRun(self):
        try:
            if _ACTIVE_ASYNCIO_OBSERVERS[-1] is not self._asyncio_observer:
                raise RuntimeError("Nested asyncio log observer ownership is inconsistent")
            _ACTIVE_ASYNCIO_OBSERVERS.pop()
            self._asyncio_logger.removeHandler(self._asyncio_observer)
        finally:
            self._active_test = None
            super().stopTestRun()

    def startTest(self, test):
        self._active_test = test
        super().startTest(test)

    def addSuccess(self, test):
        # An asyncio ERROR during a method is an error even when its body
        # returns normally. Do not append an erroneous success marker after it.
        if test not in self._asyncio_errored:
            super().addSuccess(test)

    def _record(self, kind, test, error):
        exception_type, exception, tb = error
        reason = str(exception).splitlines()
        if reason:
            reason = reason[0].strip()
        else:
            reason = ""
        if not reason:
            reason = "(no exception message)"
        if len(reason) > 240:
            reason = reason[:237] + "..."
        location = "(no traceback)"
        frames = traceback.extract_tb(tb)
        for frame in reversed(frames):
            components = Path(frame.filename).parts
            if "tests" in components:
                location = "/".join(components[components.index("tests"):]) + f":{frame.lineno}"
                break
        if location == "(no traceback)" and frames:
            frame = frames[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}"
        self.problems.append((kind, str(test), location, exception_type.__name__, reason))

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self._record("FAIL", test, error)

    def addError(self, test, error):
        super().addError(test, error)
        self._record("ERROR", test, error)

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            kind = "ERROR"
            if issubclass(error[0], test.failureException):
                kind = "FAIL"
            self._record(kind, subtest, error)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.problems.append(("UNEXPECTED PASS", str(test), "(no traceback)",
                              "UnexpectedSuccess", "A test marked as an expected failure passed."))


def _print_failure_summary(result, selection, stream):
    if result.wasSuccessful():
        return
    count = len(result.problems)
    noun = "cases"
    if count == 1:
        noun = "case"
    print(f"=== PROVIDER-CORE FAILURE SUMMARY ({selection}): {count} failing {noun} ===", file=stream)
    for kind, name, location, failure_type, reason in result.problems[:25]:
        print(f"  {kind} {name}", file=stream)
        print(f"    {location} — {failure_type}: {reason}", file=stream)
    if count > 25:
        print(f"  ... {count - 25} additional failing cases; see the full test log.", file=stream)
    print("=== END PROVIDER-CORE FAILURE SUMMARY ===", file=stream)


def main():
    prefix = Path(sys.prefix).resolve()
    origin = Path(provider_core.__file__).resolve()
    if not origin.is_relative_to(prefix):
        raise RuntimeError("provider_core was not imported from the isolated installed environment")
    distribution = importlib.metadata.distribution("provider-core")
    if not Path(distribution.locate_file("provider_core")).resolve().is_relative_to(prefix):
        raise RuntimeError("provider-core distribution metadata refers outside the installed environment")
    root = Path(__file__).parent
    selection = sys.argv[1]
    if selection not in {
        "native",
        "unit",
        "unit/normative",
        "unit/adversarial",
        "contract",
        "integration",
        "integration/chat",
        "integration/hello",
        "integration/transport",
        "integration/api",
        "integration/delivery",
    }:
        raise ValueError("Unknown provider-core test selection")
    suite = unittest.TestSuite()
    patterns = sys.argv[2:]
    if not patterns:
        raise ValueError("Provider-core discovery requires at least one explicit pattern from its maintained test launcher.")
    for pattern in patterns:
        selected = unittest.defaultTestLoader.discover(str(root / selection), pattern=pattern, top_level_dir=str(root.parent))
        if selected.countTestCases() == 0:
            raise RuntimeError(f"Selected provider-core pattern contains no tests: {pattern}")
        suite.addTests(selected)
    result = unittest.TextTestRunner(verbosity=2, resultclass=_FailureSummaryResult).run(suite)
    _print_failure_summary(result, selection, sys.stderr)
    # silent-defaults: allow-contract -- Unittest success determines the process success or failure exit status.
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
