"""Inventory authored sources visible to the contained repository lane.

The launcher pins /workspace; local conventions govern host, tools, tests, packages and
standalone examples and library mocks. Future src, packages/*/src, examples/*/src
and mocks/*/src sources enter these inventories automatically. Python fixtures
remain source: their suffix must not hide them from any applicable policy,
including checks whose ordinary module inventory is production-only.
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

    Both projections include future src, packages/*/src, examples/*/src and mocks/*/src
    modules. Production checks own host and contained tools; whole-repository checks also own tests,
    package tests and example tests. The caller must choose the check's
    responsibility. Both projections also include every public .py.fixture.
    """

    # silent-defaults: allow-contract -- The required production_only choice selects host/tools or all authored roots; future shared-package and example modules belong to both inventories; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_source_selection_respects_explicit_scope
    roots = ("host", "tools") if production_only else PYTHON_ROOTS

    patterns = [f"{name}/**/*.py" for name in (*roots, *OPTIONAL_PYTHON_ROOTS)]

    patterns.extend(("packages/*/src/**/*.py", "examples/*/src/**/*.py", "mocks/*/src/**/*.py"))

    if not production_only:
        patterns.extend(("packages/*/tests/**/*.py", "examples/*/tests/**/*.py", "mocks/*/tests/**/*.py"))

    return source_files(root=ROOT, patterns=patterns)


def repo_path(path):
    return ROOT / path


def is_skipped(path):
    return _is_skipped_relative(relative_path=path.relative_to(ROOT))


def _is_skipped_relative(*, relative_path: Path) -> bool:
    parts = relative_path.parts

    if parts and parts[0] in {"release-artifacts", "bootstrap"}:
        return True

    return any(part in SKIP_DIRS for part in parts)


def is_python_source(*, path: Path) -> bool:
    """Classify executable modules and source fixtures identically for Python rules."""

    return path.name.endswith((".py", ".py.fixture"))


# silent-defaults: allow-contract -- Empty rule-specific exclusions scan every selected public source; private exclusions always apply and Python fixtures cannot be scoped out; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_fixtures_enter_both_source_scopes_and_text_checks
def source_files(
    *,
    root: Path,
    patterns,
    exclude_parts=frozenset(),
    exclude_repo_paths=frozenset(),
    exclude_subtrees=(),
) -> list[Path]:
    """Own file discovery, private exclusions and mandatory Python-fixture coverage.

    Callers select ordinary source scope through patterns and relative exclusions.
    Any Python selection also includes every public .py.fixture, even outside
    that scope: moving a sample into a fixture must not bypass any source rule.
    Non-Python selections keep their scope. Private exclusions apply throughout;
    symlink files and ancestors cannot redirect a public name into private data.
    """
    patterns = tuple(patterns)
    paths = {path for pattern in patterns for path in root.glob(pattern)}

    fixture_paths = {
        path
        for path in root.rglob("*.py.fixture")
    } if any(is_python_source(path=Path(pattern)) for pattern in patterns) else set()

    return sorted(
        path
        for path in paths | fixture_paths
        if not _is_skipped_relative(relative_path=path.relative_to(root))
        and not any((root / ancestor).is_symlink() for ancestor in (path.relative_to(root), *path.relative_to(root).parents))
        and path.is_file()
        and (
            path in fixture_paths
            or (
                not exclude_parts.intersection(path.relative_to(root).parts)
                and path.relative_to(root) not in exclude_repo_paths
                and not any(path.relative_to(root).is_relative_to(subtree) for subtree in exclude_subtrees)
            )
        )
    )


def read_text(path):
    return path.read_text(encoding="utf-8")


def iter_repo_text_files():
    suffixes = {".py", ".sh", ".toml", ".json", ".txt", ".md", ".yaml", ".yml", ".in", ".lock"}
    names = {"Makefile", "Dockerfile", "Dockerfile.lock", ".dockerignore", ".gitignore"}

    return sorted(
        path
        for path in source_files(root=ROOT, patterns=("**/*",))
        if path.suffix in suffixes or path.name in names or is_python_source(path=path)
    )
