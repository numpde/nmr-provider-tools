"""Identify the explicit selected artifacts of a build context."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import PurePosixPath


def selection_identity(entries):
    selected = {}
    for entry in entries:
        if set(entry) != {"directory", "filename", "sha256"}:
            raise ValueError("Cannot identify selection: each entry must explicitly identify one artifact.")
        directory, name, checksum = entry["directory"], entry["filename"], entry["sha256"]
        path = PurePosixPath(directory)
        if (path.is_absolute() or str(path) != directory or not path.parts or ".." in path.parts
                or not name or name in (".", "..") or "/" in name or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in directory + name)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("Cannot identify selection: expected relative directories, filenames and SHA-256 hashes.")
        key = (directory, name)
        if key in selected and selected[key] != checksum:
            raise ValueError("Cannot identify selection: artifact entries conflict.")
        selected[key] = checksum
    canonical = [{"directory": directory, "filename": name, "sha256": checksum}
                 for (directory, name), checksum in sorted(selected.items())]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        with open(args.manifest) as stream:
            result = selection_identity(json.load(stream))
    # silent-defaults: allow-exception-outcome -- Unreadable or malformed selection identity is an explicit nonzero command failure.
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
