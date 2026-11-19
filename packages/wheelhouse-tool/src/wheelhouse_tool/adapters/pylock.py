"""Decode and serialize the standalone wheelhouse tool's pylock documents."""

import tomllib
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from packaging.pylock import Package, PackageArchive, PackageSdist, PackageWheel, Pylock
from packaging.utils import NormalizedName
from packaging.version import Version


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


def _artifact_record(url: str, sha256: str) -> dict[str, object]:
    return {"url": url, "hashes": {"sha256": sha256}}


def project_archive_record(name: NormalizedName, version: Version, url: str, sha256: str) -> dict[str, object]:
    """Encode a selected direct archive in a pylock package record."""

    return {"name": name, "version": str(version), "archive": _artifact_record(url, sha256)}


def project_index_record(
    name: NormalizedName,
    version: Version,
    *,
    wheels: Iterable[tuple[str, str]],
    sdist: tuple[str, str] | None,
) -> dict[str, object]:
    """Encode the selected index wheels and optional source distribution."""

    package: dict[str, object] = {"name": name, "version": str(version)}
    wheel_records = [_artifact_record(url, sha256) for (url, sha256) in wheels]

    if wheel_records:
        package["wheels"] = wheel_records

    if sdist is not None:
        package["sdist"] = _artifact_record(*sdist)

    return package


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
