"""Run only installed core tests; an empty selection is a qualification failure."""

from provider_core.container_guard import require_container

require_container()

import importlib.metadata
import sys
import traceback
import unittest
from pathlib import Path

import provider_core


# The complete unittest tracebacks remain above the final, bounded failure
# index. Capture the exception at the original result callback so subtest
# parameters and primary error types survive formatting and exception chaining.
class _FailureSummaryResult(unittest.TextTestResult):
    def __init__(self, *arguments, **settings):
        super().__init__(*arguments, **settings)
        self.problems = []

    def _record(self, kind, test, error):
        exception_type, exception, tb = error
        reason = str(exception).splitlines()
        reason = reason[0].strip() if reason else ""
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
            kind = "FAIL" if issubclass(error[0], test.failureException) else "ERROR"
            self._record(kind, subtest, error)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.problems.append(("UNEXPECTED PASS", str(test), "(no traceback)",
                              "UnexpectedSuccess", "A test marked as an expected failure passed."))


def _print_failure_summary(result, selection, stream):
    if result.wasSuccessful():
        return
    count = len(result.problems)
    noun = "case" if count == 1 else "cases"
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
