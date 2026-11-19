"""Inventory authored sources visible to the contained repository lane.

The launcher pins /workspace; local conventions govern host, tools, tests, packages and
standalone examples and library mocks. Future src, packages/*/src, examples/*/src
and mocks/*/src sources enter these inventories automatically. Python fixture
samples are excluded from regular source checks; dedicated scenario tests read
their samples directly.
"""

from tools.container_guard import require_container

require_container()

import pathlib

ROOT = pathlib.Path("/workspace")
REPOSITORY_ROOT = ROOT
PYTHON_ROOTS = ("host", "operations", "tools", "tests")
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
    responsibility. Fixture samples belong to dedicated scenario tests.
    """

    # silent-defaults: allow-contract -- The required production_only choice selects host/tools or all authored roots; future shared-package and example modules belong to both inventories; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_source_selection_respects_explicit_scope
    roots = ("host", "operations", "tools") if production_only else PYTHON_ROOTS

    patterns = [f"{name}/**/*.py" for name in (*roots, *OPTIONAL_PYTHON_ROOTS)]

    patterns.extend(
        (
            "packages/*/src/**/*.py",
            "packages/*/build/**/*.py",
            "packages/provider-core/sdk_build/**/*.py",
            "packages/acquisition/acquisition_tool/**/*.py",
            "examples/*/src/**/*.py",
            "mocks/*/src/**/*.py",
        ),
    )

    if not production_only:
        patterns.extend(("packages/*/tests/**/*.py", "examples/*/tests/**/*.py", "mocks/*/tests/**/*.py"))

    return source_files(root=ROOT, patterns=patterns)


def repo_path(repo_relative_path):
    return ROOT / repo_relative_path


def is_skipped(source_path):
    return _is_skipped_relative(repo_relative_path=source_path.relative_to(ROOT))


def _is_skipped_relative(*, repo_relative_path: pathlib.Path) -> bool:
    parts = repo_relative_path.parts

    if parts and (parts[0] in {"release-artifacts", "bootstrap"}):
        return True

    return any(part in SKIP_DIRS for part in parts)


def is_python_source(*, source_path: pathlib.Path) -> bool:
    """Recognize Python modules and samples for direct detector dispatch."""

    return source_path.name.endswith((".py", ".py.fixture"))


# silent-defaults: allow-contract -- Empty rule-specific exclusions scan every selected public source; private exclusions and fixture-sample exclusions always apply; proof=tests/repository/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_fixtures_are_excluded_from_regular_source_checks
def source_files(
    *,
    root: pathlib.Path,
    patterns,
    exclude_parts=frozenset(),
    exclude_repo_paths=frozenset(),
    exclude_subtrees=(),
) -> list[pathlib.Path]:
    """Discover regular source files within the caller's selected scope.

    Fixture samples remain available to dedicated scenario tests through direct
    reads. Private exclusions apply throughout; symlink files and ancestors
    cannot redirect a public name into private data.
    """
    candidates = {candidate for pattern in patterns for candidate in root.glob(pattern)}

    return sorted(
        candidate
        for candidate in candidates
        if
        (not candidate.name.endswith(".py.fixture")) and
        (not _is_skipped_relative(repo_relative_path=candidate.relative_to(root))) and
        (not any(
            (
                (root / ancestor).is_symlink()
                for ancestor in (candidate.relative_to(root), *candidate.relative_to(root).parents)
            ),
        )) and
        candidate.is_file() and
        (not exclude_parts.intersection(candidate.relative_to(root).parts)) and
        (candidate.relative_to(root) not in exclude_repo_paths) and
        (not any(candidate.relative_to(root).is_relative_to(subtree) for subtree in exclude_subtrees))
    )


def read_text(text_path):
    return text_path.read_text(encoding="utf-8")


def iter_repo_text_files():
    suffixes = {".py", ".sh", ".toml", ".json", ".txt", ".md", ".yaml", ".yml", ".in", ".lock"}
    names = {"Makefile", "Dockerfile", "Dockerfile.lock", ".dockerignore", ".gitignore"}

    return sorted(
        source_path
        for source_path in source_files(root=ROOT, patterns=("**/*",))
        if (source_path.suffix in suffixes) or (source_path.name in names) or is_python_source(source_path=source_path)
    )
