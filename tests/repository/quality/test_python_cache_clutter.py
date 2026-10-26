# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_python_cache_clutter.py
# Original detector fixtures and repository assertions retained; source roots alone are local.
"""Repository posture checks for host Python cache artifacts.

Routine verification is Docker-backed, but occasional host-side Python commands
can still leave ignored bytecode behind. This check keeps those artifacts
visible so the worktree shape remains reproducible and easy to inspect.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import unittest
from pathlib import Path

from .sources import REPOSITORY_ROOT

PYTHON_CACHE_ROOTS = ("tools", "tests")
PYTHON_TOOL_CACHE_DIRS = (
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


def _python_cache_artifacts() -> list[str]:
    artifacts: list[Path] = []
    for cache_name in PYTHON_TOOL_CACHE_DIRS:
        cache_path = REPOSITORY_ROOT / cache_name
        if cache_path.exists():
            artifacts.append(cache_path)

    for root_name in (*PYTHON_CACHE_ROOTS, "src", "packages"):
        root = REPOSITORY_ROOT / root_name
        if not root.exists():
            continue
        artifacts.extend(root.rglob("__pycache__"))
        artifacts.extend(root.rglob("*.pyc"))
        artifacts.extend(root.rglob("*.pyo"))
        for cache_name in PYTHON_TOOL_CACHE_DIRS:
            artifacts.extend(root.rglob(cache_name))

    return sorted(path.relative_to(REPOSITORY_ROOT).as_posix() for path in artifacts)


class PythonCacheClutterTests(unittest.TestCase):
    maxDiff = None

    def test_scanned_python_roots_exist(self) -> None:
        missing_roots = [root_name for root_name in PYTHON_CACHE_ROOTS if not (REPOSITORY_ROOT / root_name).is_dir()]

        self.assertEqual([], missing_roots)

    def test_python_surfaces_do_not_contain_cache_artifacts(self) -> None:
        self.assertEqual([], _python_cache_artifacts())


if __name__ == "__main__":
    unittest.main()
