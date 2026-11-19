"""Decode and serialize the standalone wheelhouse tool's pylock documents."""

import tomllib
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from packaging.pylock import Package, PackageArchive, PackageSdist, PackageWheel, Pylock
from packaging.utils import NormalizedName


def read_lock(lock: Path) -> Pylock:
    """Decode a TOML lock with the pinned packaging implementation."""

    with lock.open("rb") as stream:
        return Pylock.from_dict(tomllib.load(stream))


def render_local_lock(
    document: Pylock,
    selections: Iterable[tuple[Package, PackageArchive | PackageSdist | PackageWheel, Path]],
) -> str:
    """Encode selected artifact paths in a copy of the original lock."""

    import tomli_w

    packages = []

    for (package, artifact, local_path) in selections:
        local = replace(artifact, url=None, path=str(local_path))

        if isinstance(local, PackageWheel):
            package = replace(package, wheels=[local], sdist=None)
        elif isinstance(local, PackageSdist):
            package = replace(package, wheels=None, sdist=local)
        else:
            package = replace(package, archive=local)

        packages.append(package)

    return tomli_w.dumps(replace(document, packages=packages).to_dict())


def render_projected_lock(
    packages: list[dict[str, object]],
    *,
    created_by: str,
    tool: dict[str, object],
    prefer_sdist_predicate: Callable[[NormalizedName], bool],
) -> str:
    """Prove that the projected lock selects artifacts before encoding it."""

    import tomli_w

    document = {
        "lock-version": "1.0",
        "created-by": created_by,
        "packages": packages,
        "tool": tool,
    }

    list(Pylock.from_dict(document).select(prefer_sdist_predicate=prefer_sdist_predicate))

    return tomli_w.dumps(document)
