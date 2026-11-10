"""Inventory authored sources visible to the contained repository lane.

The launcher pins /workspace; local conventions govern host, tools, tests, packages and
standalone examples. Future src, packages/*/src and examples/*/src sources enter
these inventories automatically.
"""

from tools.container_guard import require_container

require_container()

from pathlib import Path

ROOT = Path("/workspace")
REPOSITORY_ROOT = ROOT
PYTHON_ROOTS = ("host", "tools", "tests")
OPTIONAL_PYTHON_ROOTS = ("src",)
SKIP_DIRS = {
    ".git",
    ".agents",
    ".codex",
    ".aws",
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

    Both projections include future src, packages/*/src and examples/*/src
    modules. Production checks own host and contained tools; whole-repository checks also own tests,
    package tests and example tests. The caller must choose the check's
    responsibility.
    """
    # silent-defaults: allow-contract -- The required production_only choice selects host/tools or all authored roots; future shared-package and example modules belong to both inventories; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_source_selection_respects_explicit_scope
    roots = ("host", "tools") if production_only else PYTHON_ROOTS
    directories = [ROOT / name for name in (*roots, *OPTIONAL_PYTHON_ROOTS)]
    directories.extend(ROOT.glob("packages/*/src"))
    directories.extend(ROOT.glob("examples/*/src"))
    if not production_only:
        directories.extend(ROOT.glob("packages/*/tests"))
        directories.extend(ROOT.glob("examples/*/tests"))
    return sorted(path for directory in directories for path in directory.rglob("*.py"))


def repo_path(path):
    return ROOT / path


def is_skipped(path):
    parts = path.relative_to(ROOT).parts
    if parts and parts[0] in {"release-artifacts", "bootstrap"}:
        return True
    return any(part in SKIP_DIRS for part in parts)


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
