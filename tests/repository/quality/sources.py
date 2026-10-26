"""Inventory authored sources visible to the contained repository lane.

The launcher pins /workspace; local conventions govern tools, tests and packages.
Future src and packages/*/src sources enter these inventories automatically.
"""

from tools.container_guard import require_container

require_container()

from pathlib import Path

ROOT = Path("/workspace")
REPOSITORY_ROOT = ROOT
PYTHON_ROOTS = ("tools", "tests")
OPTIONAL_PYTHON_ROOTS = ("src",)
SKIP_DIRS = {
    ".git",
    ".agents",
    ".codex",
    ".aws",
    ".work",
    "secrets",
    "node_modules",
    "dependencies",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def python_sources(*, production_only):
    """Use the caller's explicit production or whole-repository inventory.

    Both projections include future src and packages/*/src modules. Production
    checks own tools; whole-repository checks also own tests and package tests.
    The caller must choose the check's responsibility.
    """
    # silent-defaults: allow-contract -- The required production_only choice selects tools or all authored roots; future shared-package modules belong to both inventories; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_source_selection_respects_explicit_scope
    roots = ("tools",) if production_only else PYTHON_ROOTS
    directories = [ROOT / name for name in (*roots, *OPTIONAL_PYTHON_ROOTS)]
    directories.extend(ROOT.glob("packages/*/src"))
    if not production_only:
        directories.extend(ROOT.glob("packages/*/tests"))
    return sorted(path for directory in directories for path in directory.rglob("*.py"))


def repo_path(path):
    return ROOT / path


def is_skipped(path):
    return any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)


def read_text(path):
    return path.read_text(encoding="utf-8")


def iter_repo_text_files():
    suffixes = {".py", ".sh", ".toml", ".json", ".txt", ".md", ".yaml", ".yml", ".in", ".lock"}
    names = {"Makefile", "Dockerfile", "Dockerfile.lock", ".dockerignore", ".gitignore"}
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not is_skipped(path) and (path.suffix in suffixes or path.name in names)
    )
