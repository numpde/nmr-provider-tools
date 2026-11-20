"""Admit pip-reported files under the offline pool's selection policy."""

import re
import reprlib
import urllib.parse
from pathlib import PurePosixPath

from wheelhouse_tool.validation.upstream import pip_installation_report


def local_file_path(item):
    """Decode a local artifact URL, rejecting ambiguous paths before decoding."""

    artifact_url = pip_installation_report.artifact_url(item)

    if any(((ord(character) < 32) or (ord(character) == 127)) for character in artifact_url):
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
        raise ValueError("install_records should not be empty")

    pool_path = PurePosixPath(pool)

    if not pool_path.is_absolute():
        raise ValueError(f"{reprlib.repr(pool_path)=} should be absolute")

    artifact_entries = []
    seen_filenames = set()

    for installation_record in install_records:
        artifact_path = local_file_path(installation_record)
        candidate = PurePosixPath(artifact_path)
        artifact_filename = candidate.name

        if (
            (candidate.parent != pool_path) or  # Select files directly inside the admitted pool
            (str(candidate) != artifact_path) or  # Reject alternate spellings such as a/./b or a//b
            (not artifact_filename) or  # Require a filename
            (artifact_filename in (".", "..")) or  # A directory reference cannot name an artifact
            ("\\" in artifact_filename) or  # Reject Windows path separators too
            any(((ord(character) < 32) or (ord(character) == 127)) for character in artifact_filename) or  # Keep control characters out of artifact names
            ((not artifact_filename.endswith(".whl")) and (artifact_filename not in allowed_sources))  # Source archives need explicit authorization
        ):
            raise ValueError(f"{reprlib.repr(artifact_path)=} should be a canonical path to an authorized wheel or source file directly in {reprlib.repr(pool_path)=}")

        artifact_sha256 = pip_installation_report.archive_sha256(installation_record)

        if not re.fullmatch((expected_sha256_pattern := r"[0-9a-f]{64}"), artifact_sha256):
            raise ValueError(f"{reprlib.repr(artifact_sha256)=} should match {expected_sha256_pattern=}")

        if not (artifact_filename not in seen_filenames):
            raise ValueError(f"{reprlib.repr(artifact_filename)=} should occur only once")

        seen_filenames.add(artifact_filename)
        artifact_entries.append({"filename": artifact_filename, "sha256": artifact_sha256, "directory": directory})

    return artifact_entries
