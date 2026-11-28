"""Run only installed core tests; an empty selection is a qualification failure."""

from provider_core.container_guard import require_container

require_container()

import importlib.metadata
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import provider_core


# The installed runner is executed as a script (__main__), while its own
# regression tests import tests.run_suite. Both names must identify the same
# module or two independent observer stacks will count nested test errors twice.
if __name__ == "__main__":
    sys.modules["tests.run_suite"] = sys.modules[__name__]


# Observe the asyncio logger for the complete unittest runner lifetime,
# not merely between startTest and stopTest. IsolatedAsyncioTestCase closes its loop
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
    def __init__(self, suite_result):
        super().__init__(level=logging.ERROR)
        self.suite_result = suite_result

    def emit(self, record):
        if (not _ACTIVE_ASYNCIO_OBSERVERS) or (_ACTIVE_ASYNCIO_OBSERVERS[-1] is not self):
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
                context += (
                    "\n"
                    "... further asyncio context truncated"
                )

            diagnostic.add_note("Original asyncio event context:\n" + context)

        case = self.suite_result._active_test

        if case is None:
            case = self.suite_result._unattributed_asyncio_test

        try:
            raise diagnostic
        except _AsyncioDiagnosticError:
            self.suite_result.addError(case, sys.exc_info())
            self.suite_result._asyncio_errored.add(case)


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
            if not (_ACTIVE_ASYNCIO_OBSERVERS[-1] is self._asyncio_observer):
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
        (exception_type, exception, tb) = error
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
            components = pathlib.Path(frame.filename).parts

            if "tests" in components:
                location = "/".join(components[components.index("tests"):]) + f":{frame.lineno}"

                break

        if (location == "(no traceback)") and frames:
            frame = frames[-1]
            location = f"{pathlib.Path(frame.filename).name}:{frame.lineno}"

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

        self.problems.append(
            (
                "UNEXPECTED PASS",
                str(test),
                "(no traceback)",
                "UnexpectedSuccess",
                "A test marked as an expected failure passed."
            )
        )


def _print_failure_summary(suite_result, selection, stream):
    if suite_result.wasSuccessful():
        return

    count = len(suite_result.problems)
    noun = "cases"

    if count == 1:
        noun = "case"

    print(f"=== PROVIDER-CORE FAILURE SUMMARY ({selection}): {count} failing {noun} ===", file=stream)

    for (kind, name, location, failure_type, reason) in suite_result.problems[:25]:
        print(f"  {kind} {name}", file=stream)
        print(f"    {location} — {failure_type}: {reason}", file=stream)

    if count > 25:
        print(f"  ... {count - 25} additional failing cases; see the full test log.", file=stream)

    print("=== END PROVIDER-CORE FAILURE SUMMARY ===", file=stream)


def _modules(suite):
    """Keep module fixtures together; each module belongs to exactly one worker."""

    modules = {}
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            for module, identities in _modules(test).items():
                modules.setdefault(module, []).extend(identities)
        else:
            modules.setdefault(type(test).__module__, []).append(test.id())
    return modules


def _run_module(module, identities, selection, stopping):
    """Run the existing installed-package reporter in an isolated process group."""

    started = time.monotonic()
    if stopping.is_set():
        return module, 1, 0, "Module cancelled before launch\n"
    with tempfile.TemporaryFile(mode="w+t") as output:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", __file__, selection, "--module", module, *identities],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            status = 1
            while not stopping.is_set():
                if time.monotonic() - started >= 300:
                    print(f"Module {module} exceeded its 300-second budget", file=output)
                    break
                try:
                    status = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # Also retire descendants left after a worker exits. Module tests
            # retain their own resource cleanup; this bounds the runner's group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        output.seek(0)
        return module, status, time.monotonic() - started, output.read()


def _run_parallel(suite, selection, workers):
    started = time.monotonic()
    modules = _modules(suite)
    print(f"Running {suite.countTestCases()} tests in {len(modules)} modules with {workers} workers", flush=True)
    failed = []
    stopping = threading.Event()
    pool = ThreadPoolExecutor(max_workers=workers)
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt("Parallel test runner interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        pending = [pool.submit(_run_module, module, identities, selection, stopping)
                   for module, identities in sorted(modules.items())]
        for completed in as_completed(pending):
            module, status, elapsed, report = completed.result()
            print(report, end="", flush=True)
            print(f"MODULE_RESULT {module} status={status} seconds={elapsed:.3f}", flush=True)
            if status:
                failed.append(module)
    finally:
        stopping.set()
        pool.shutdown(wait=True, cancel_futures=True)
        signal.signal(signal.SIGTERM, previous_term)
        print(f"SUITE_TIME {selection} mode=parallel workers={workers} seconds={time.monotonic() - started:.3f}", flush=True)
    if failed:
        print("=== PROVIDER-CORE FAILURE SUMMARY (parallel modules) ===", file=sys.stderr)
        for module in sorted(failed):
            print(f"  FAIL {module}; full worker report above", file=sys.stderr)
        print("=== END PROVIDER-CORE FAILURE SUMMARY ===", file=sys.stderr)
    return 1 if failed else 0


def main():
    installed_environment = pathlib.Path(sys.prefix).resolve()
    imported_package_path = pathlib.Path(provider_core.__file__).resolve()

    if not imported_package_path.is_relative_to(installed_environment):
        raise RuntimeError(f"{imported_package_path=} should be under {installed_environment=}")

    distribution = importlib.metadata.distribution("provider-core")
    distribution_package_path = pathlib.Path(distribution.locate_file("provider_core")).resolve()

    if not distribution_package_path.is_relative_to(installed_environment):
        raise RuntimeError(f"{distribution_package_path=} should be under {installed_environment=}")

    root = pathlib.Path(__file__).parent
    selection = sys.argv[1]

    if not (selection in {
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
    }):
        raise ValueError("Unknown provider-core test selection")

    suite = unittest.TestSuite()
    patterns = sys.argv[2:]

    if patterns[:1] == ["--module"]:
        if len(patterns) < 3 or not patterns[1].startswith("tests." + selection.replace("/", ".") + "."):
            raise ValueError("Module worker must select one module within its suite")
        if any(not identity.startswith(patterns[1] + ".") for identity in patterns[2:]):
            raise ValueError("Module worker test identities must belong to its module")
        sys.path.insert(0, str(root.parent))
        suite = unittest.defaultTestLoader.loadTestsFromNames(patterns[2:])
        patterns = []
    else:
        if not patterns:
            raise ValueError("Provider-core discovery requires an explicit test pattern")

    for pattern in patterns:
        selected = unittest.defaultTestLoader.discover(str(root / selection), pattern=pattern, top_level_dir=str(root.parent))

        if selected.countTestCases() == 0:
            raise RuntimeError(f"Selected provider-core pattern contains no tests: {pattern}")

        suite.addTests(selected)

    if suite.countTestCases() == 0:
        raise RuntimeError("Selected provider-core suite contains no tests")

    workers = os.environ.get("PROVIDER_CORE_TEST_WORKERS", "1")
    if workers not in {"1", "2", "3", "4"}:
        raise ValueError("PROVIDER_CORE_TEST_WORKERS must be 1 through 4")
    if patterns and selection == "unit" and workers != "1" and not unittest.defaultTestLoader.errors:
        return _run_parallel(suite, selection, int(workers))

    started = time.monotonic()
    try:
        suite_result = unittest.TextTestRunner(verbosity=2, resultclass=_FailureSummaryResult).run(suite)
    finally:
        print(f"SUITE_TIME {selection} mode=serial seconds={time.monotonic() - started:.3f}", flush=True)

    _print_failure_summary(suite_result, selection, sys.stderr)

    # silent-defaults: allow-contract -- Unittest success determines the process success or failure exit status.
    return 0 if suite_result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
