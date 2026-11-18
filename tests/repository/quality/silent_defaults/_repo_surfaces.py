# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/_repo_surfaces.py
# Original exclusion validation and line classification; no inherited project secrets.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import pathlib
from collections.abc import Iterable, Iterator

from ..sources import ROOT as REPO_ROOT, source_files

INTERPOLATION_DEFAULT_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[#$!?*@_-])"
    r"(?P<operator>:?[-=])(?P<value>[^}]*)\}"
)

SILENT_DEFAULTS_TEST_PARTS = frozenset({"silent_defaults"})

# The original private deployment files do not exist here. No local exclusions.
SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS = frozenset()


def repo_path(source_path: pathlib.Path) -> pathlib.Path:
    return source_path.relative_to(REPO_ROOT)


def repo_source(source_path: pathlib.Path) -> str:
    return repo_path(source_path).as_posix()


def _validate_repo_relative_paths(paths: frozenset[pathlib.Path]) -> None:
    absolute_paths = sorted(excluded_path.as_posix() for excluded_path in paths if excluded_path.is_absolute())

    if absolute_paths:
        raise ValueError(f"surface file exclusions must be repo-relative: {absolute_paths}")


def surface_files(
    patterns: Iterable[str],
    *,
    extra_exclude_parts: frozenset[str] = frozenset(),
    exclude_repo_paths: frozenset[pathlib.Path] = frozenset(),
) -> list[pathlib.Path]:
    _validate_repo_relative_paths(exclude_repo_paths)

    return source_files(
        root=REPO_ROOT,
        patterns=patterns,
        exclude_parts=SILENT_DEFAULTS_TEST_PARTS | extra_exclude_parts,
        exclude_repo_paths=exclude_repo_paths,
    )


def surface_lines(
    paths: Iterable[pathlib.Path],
    *,
    skip_comments: bool = False,
) -> Iterator[tuple[str, int, str]]:
    for source_path in paths:
        source = repo_source(source_path)

        for (line_number, line) in enumerate(
            source_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if skip_comments and line.lstrip().startswith("#"):
                continue

            yield (source, line_number, line)
