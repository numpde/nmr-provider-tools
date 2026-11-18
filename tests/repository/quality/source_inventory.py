# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/_source_inventory.py
# Complete original discovery, structural rejection and exact inventory semantics.
"""Exact source-tree discovery for repository posture checks."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import pathlib
from pathlib import PurePosixPath


class SourceInventoryError(AssertionError):
    """A security-sensitive source tree is incomplete or structurally unsafe."""


def _validated_subtree(subtree: str) -> PurePosixPath:
    if not isinstance(subtree, (expected_type := str)):
        raise TypeError(f"{type(subtree)=} ≠ {expected_type=}")

    candidate = PurePosixPath(subtree)

    if (
        not subtree
        or candidate.is_absolute()
        or candidate.as_posix() != subtree
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError("subtree must be a normalized relative POSIX path")

    return candidate


def discover_source_files(*, root: pathlib.Path, subtree: str) -> tuple[str, ...]:
    """Return every regular file below ``subtree`` in deterministic order.

    The local checker reads the reviewed source trees through read-only mounts.
    The host can still change those files; this discovery does not freeze their
    bytes. Symlinks are rejected before regular-file checks so inventory
    membership identifies content physically contained by the reviewed root.
    Committed immutable context proof belongs to the scientific source checks.
    """

    if not isinstance(root, expected_type := pathlib.Path):
        raise TypeError(f"{root=} does not have {expected_type=}")

    subtree_path = _validated_subtree(subtree)
    base = root.joinpath(*subtree_path.parts)

    if base.is_symlink():
        raise SourceInventoryError(f"{subtree} source root must not be a symlink")

    if not base.is_dir():
        raise SourceInventoryError(f"{subtree} source root must be a directory")

    discovered: list[str] = []

    for source_path in sorted(base.rglob("*"), key=lambda source_path: source_path.as_posix()):
        repo_relative_path = source_path.relative_to(root).as_posix()

        # todo: double check logic

        if source_path.is_symlink():
            raise SourceInventoryError(f"{repo_relative_path=} must not be a symlink")

        if source_path.is_dir():
            continue

        if not source_path.is_file():
            raise SourceInventoryError(f"{repo_relative_path=} must be a regular file")

        discovered.append(repo_relative_path)

    return tuple(discovered)


def inventory_delta(
    *,
    discovered: tuple[str, ...] | frozenset[str],
    reviewed: tuple[str, ...] | frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return sorted ``(missing, unclassified)`` inventory differences."""

    discovered_set = frozenset(discovered)
    reviewed_set = frozenset(reviewed)

    return (
        tuple(sorted(reviewed_set - discovered_set)),
        tuple(sorted(discovered_set - reviewed_set)),
    )


def require_exact_inventory(
    *,
    label: str,
    discovered: tuple[str, ...] | frozenset[str],
    reviewed: tuple[str, ...] | frozenset[str],
) -> None:
    """Reject missing reviewed files and present-but-unclassified files."""

    if not isinstance(label, str) or not label:
        raise TypeError("label must be a non-empty string")

    (missing, unclassified) = inventory_delta(
        discovered=discovered,
        reviewed=reviewed,
    )

    if missing or unclassified:
        raise SourceInventoryError(f"{label} inventory is not exact; missing={list(missing)!r}; unclassified={list(unclassified)!r}")
