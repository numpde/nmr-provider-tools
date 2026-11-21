"""Admit build-context artifact identities before canonical hashing."""

import re
import reprlib
from pathlib import PurePosixPath


def selected_artifacts(entries):
    """Return artifact hashes, merging identical repeats and rejecting conflicts."""

    selected = {}

    for entry in entries:
        entry_fields = set(entry)
        expected_entry_fields = {"directory", "filename", "sha256"}

        if not (entry_fields == expected_entry_fields):
            raise ValueError(f"{reprlib.repr(entry_fields)=} should match {expected_entry_fields=}")

        (artifact_directory, artifact_filename, artifact_sha256) = (entry["directory"], entry["filename"], entry["sha256"])
        artifact_directory_path = PurePosixPath(artifact_directory)

        if (
            artifact_directory_path.is_absolute() or  # Locate artifacts relative to the build context
            (str(artifact_directory_path) != artifact_directory) or  # Reject alternate spellings such as a/./b or a//b
            (not artifact_directory_path.parts) or  # Require a named directory below the context root
            (".." in artifact_directory_path.parts) or  # Prevent traversal into a parent directory
            (not artifact_filename) or  # Require a filename
            (artifact_filename in (".", "..")) or  # A directory reference cannot name an artifact
            ("/" in artifact_filename) or  # Keep subdirectories in the directory field
            ("\\" in artifact_filename) or  # Reject Windows path separators too
            any(((ord(character) < 32) or (ord(character) == 127)) for character in artifact_directory + artifact_filename)  # Keep control characters out of artifact paths
        ):
            raise ValueError(
                f"{reprlib.repr(artifact_directory)=} and {reprlib.repr(artifact_filename)=} "
                f"should form a canonical relative artifact path without control characters or traversal"
            )

        # Require a complete lowercase SHA-256 digest
        expected_sha256_pattern = r"[0-9a-f]{64}"

        if not re.fullmatch(expected_sha256_pattern, artifact_sha256):
            raise ValueError(f"{reprlib.repr(artifact_sha256)=} should match {expected_sha256_pattern=}")

        artifact_key = (artifact_directory, artifact_filename)

        if (artifact_key in selected) and (selected[artifact_key] != artifact_sha256):
            raise ValueError(f"{artifact_sha256=} should match {selected[artifact_key]=} for {reprlib.repr(artifact_key)=}")

        selected[artifact_key] = artifact_sha256

    return selected
