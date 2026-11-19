"""Read bootstrap pylock fields and encode delivery archive records."""

from tools.container_guard import require_container

require_container()

import json
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


def packages(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read package records without choosing how many the caller accepts."""

    return document["packages"]


def wheels(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read wheel records without selecting an artifact."""

    return package["wheels"]


def package_name(package: Mapping[str, Any]) -> str:
    """Read the name for caller-owned package identity admission."""

    return package["name"]


def package_version(package: Mapping[str, Any]) -> str:
    """Read the version for caller-owned release identity admission."""

    return package["version"]


def artifact_url(artifact: Mapping[str, Any]) -> str:
    """Read the location before caller-owned file selection."""

    return artifact["url"]


def artifact_sha256(artifact: Mapping[str, Any]) -> str:
    """Read the expected hash at the caller's admission point."""

    return artifact["hashes"]["sha256"]


def write_wheel_archive_lock(
    stream: TextIO,
    artifacts: Iterable[tuple[str, str, str]],
    *,
    created_by: str,
) -> None:
    """Write wheel identities as pylock 1.0 archive records in input order."""

    from pip._vendor.packaging.utils import parse_wheel_filename

    print('lock-version = "1.0"', file=stream)
    print(
        f"created-by = {json.dumps(created_by)}",  # machine-string: pylock values cannot contain debug expression names
        file=stream,
    )

    for (wheel_name, checksum, url) in artifacts:
        (distribution, version, _, _) = parse_wheel_filename(wheel_name)

        print("[[packages]]", file=stream)
        print(
            f"name = {json.dumps(str(distribution))}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )
        print(
            f"version = {json.dumps(str(version))}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )

        print("[packages.archive]", file=stream)
        print(
            f"url = {json.dumps(url)}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )

        print("[packages.archive.hashes]", file=stream)
        print(
            f"sha256 = {json.dumps(checksum)}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )
