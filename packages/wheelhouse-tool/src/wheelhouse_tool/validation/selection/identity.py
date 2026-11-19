"""Admit build-context artifact identities before canonical hashing."""

import re
from pathlib import PurePosixPath


def selected_artifacts(entries):
    """Return artifact hashes, merging identical repeats and rejecting conflicts."""

    selected = {}

    for entry in entries:
        if set(entry) != {"directory", "filename", "sha256"}:
            raise ValueError("Cannot identify selection: each entry must explicitly identify one artifact.")

        (directory, name, checksum) = (entry["directory"], entry["filename"], entry["sha256"])
        path = PurePosixPath(directory)

        if (
            path.is_absolute() or  # Locate artifacts relative to the build context
            (str(path) != directory) or  # Reject alternate spellings such as a/./b or a//b
            (not path.parts) or  # Require a named directory below the context root
            (".." in path.parts) or  # Prevent traversal into a parent directory
            (not name) or  # Require a filename
            (name in (".", "..")) or  # A directory reference cannot name an artifact
            ("/" in name) or  # Keep subdirectories in the directory field
            ("\\" in name) or  # Reject Windows path separators too
            any((ord(character) < 32) or (ord(character) == 127) for character in directory + name) or  # Keep control characters out of artifact paths
            (not re.fullmatch(r"[0-9a-f]{64}", checksum))  # Require a complete lowercase SHA-256 digest
        ):
            raise ValueError("Cannot identify selection: expected relative directories, filenames and SHA-256 hashes.")

        key = (directory, name)

        if (key in selected) and (selected[key] != checksum):
            raise ValueError("Cannot identify selection: artifact entries conflict.")

        selected[key] = checksum

    return selected
