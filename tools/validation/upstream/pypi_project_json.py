"""Read the consumed PyPI release fields without admitting download policy.

https://docs.pypi.org/api/json/#get-release
Keep field reads separate so callers can reject a filename before accessing its
digest, and a digest before accessing its URL. Missing fields retain native errors.
"""

from tools.container_guard import require_container

require_container()

from collections.abc import Mapping
from typing import Any


def release_files(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read the files for this release, retaining their received order."""

    return metadata["urls"]


def file_name(release_file: Mapping[str, Any]) -> str:
    """Read the filename before the caller selects a pinned artifact."""

    return release_file["filename"]


def file_sha256(release_file: Mapping[str, Any]) -> str:
    """Read the declared SHA-256 before the caller compares its approved pin."""

    return release_file["digests"]["sha256"]


def file_url(release_file: Mapping[str, Any]) -> str:
    """Read the selected file's URL before the caller admits its download host."""

    return release_file["url"]
