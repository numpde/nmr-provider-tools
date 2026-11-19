"""Admit pip-reported files under the offline pool's selection policy."""

import re
import reprlib
import urllib.parse
from pathlib import PurePosixPath

from wheelhouse_tool.validation.upstream import pip_installation_report


def local_file_path(item):
    """Decode a local artifact URL, rejecting ambiguous paths before decoding."""

    artifact_url = pip_installation_report.artifact_url(item)

    if any(ord(character) < 32 or ord(character) == 127 for character in artifact_url):
        raise ValueError(f"{reprlib.repr(artifact_url)=} contains a control character")

    url = urllib.parse.urlsplit(artifact_url)

    if (
        (url.scheme != "file") or  # Only local files belong in this offline manifest
        url.netloc or  # Reject file URLs that name a host
        url.query or  # A query cannot identify part of a local filename
        url.fragment or  # A fragment cannot identify part of a local filename
        re.search(r"%(?![0-9a-fA-F]{2})", url.path)  # Each percent escape must encode one byte
    ):
        raise ValueError(f"{reprlib.repr(artifact_url)=} should use file: without an authority, query, fragment or malformed percent escapes")

    try:
        return urllib.parse.unquote(url.path, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{reprlib.repr(artifact_url)=} should have a valid UTF-8 path") from error


def selected_artifacts(report, pool, directory, allowed_sources):
    """Admit pool artifacts in report order, checking authorization before hashes.

    The caller owns the successful hash-checked pip run and its frozen lock.
    """

    install_records = pip_installation_report.install_records(report)

    if not install_records:
        raise ValueError("Cannot project a pip report with no installation records.")

    root = PurePosixPath(pool)

    if not root.is_absolute():
        raise ValueError("Cannot use pip report: pool must be an absolute path in the pip environment.")

    entries = []
    seen = set()

    for item in install_records:
        path = local_file_path(item)
        candidate = PurePosixPath(path)
        name = candidate.name

        if (
            (candidate.parent != root) or  # Select files directly inside the admitted pool
            (str(candidate) != path) or  # Reject alternate spellings such as a/./b or a//b
            (not name) or  # Require a filename
            (name in (".", "..")) or  # A directory reference cannot name an artifact
            ("\\" in name) or  # Reject Windows path separators too
            any(ord(character) < 32 or ord(character) == 127 for character in name) or  # Keep control characters out of artifact names
            ((not name.endswith(".whl")) and (name not in allowed_sources))  # Source archives need explicit authorization
        ):
            raise ValueError("Cannot use pip report: selected artifact is outside the pool or not authorized.")

        checksum = pip_installation_report.archive_sha256(item)

        if (
            (not re.fullmatch(r"[0-9a-f]{64}", checksum)) or  # Require a complete lowercase SHA-256 digest
            (name in seen)  # Each filename must identify only one report entry
        ):
            raise ValueError("Cannot use pip report: selected artifact hash or filename is invalid.")

        seen.add(name)
        entries.append({"filename": name, "sha256": checksum, "directory": directory})

    return entries
