"""Run only installed core tests; an empty selection is a qualification failure."""

from provider_core.container_guard import require_container

require_container()

import importlib.metadata
import sys
import unittest
from pathlib import Path

import provider_core


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
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # silent-defaults: allow-contract -- Unittest success determines the process success or failure exit status.
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
