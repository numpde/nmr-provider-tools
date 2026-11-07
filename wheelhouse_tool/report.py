"""Project an owned offline pip report into shared stream inputs."""

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import PurePosixPath


def manifest(report, pool, directory, allowed_sources):
    """The caller owns the successful hash-checked pip run and its frozen lock."""
    if not isinstance(report, dict) or report.get("version") != "1" or not isinstance(report.get("install"), list) or not report["install"]:
        raise ValueError("Cannot use pip report: expected a nonempty version 1 installation report.")
    root = PurePosixPath(pool)
    if not root.is_absolute():
        raise ValueError("Cannot use pip report: pool must be an absolute path in the pip environment.")
    entries = []
    seen = set()
    for item in report["install"]:
        download = item["download_info"]
        raw_url = download["url"]
        if any(ord(character) < 32 or ord(character) == 127 for character in raw_url):
            raise ValueError("Cannot use pip report: selected artifact URL contains control characters.")
        url = urllib.parse.urlsplit(raw_url)
        if (url.scheme != "file" or url.netloc or url.query or url.fragment
                or re.search(r"%(?![0-9a-fA-F]{2})", url.path)):
            raise ValueError("Cannot use pip report: selected artifact is not a local pool file.")
        try:
            path = urllib.parse.unquote(url.path, errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("Cannot use pip report: selected artifact path is invalid.") from error
        candidate = PurePosixPath(path)
        name = candidate.name
        if (candidate.parent != root or str(candidate) != path or not name or name in (".", "..") or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or (not name.endswith(".whl") and name not in allowed_sources)):
            raise ValueError("Cannot use pip report: selected artifact is outside the pool or not authorized.")
        checksum = download["archive_info"]["hashes"]["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", checksum) or name in seen:
            raise ValueError("Cannot use pip report: selected artifact hash or filename is invalid.")
        seen.add(name)
        entries.append({"filename": name, "sha256": checksum, "directory": directory})
    return sorted(entries, key=lambda entry: entry["filename"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--allow-source", action="append")
    args = parser.parse_args()
    allowed = []
    if args.allow_source is not None:
        allowed = args.allow_source
    try:
        with open(args.report) as source:
            entries = manifest(json.load(source), args.pool, args.directory, set(allowed))
    # silent-defaults: allow-exception-outcome -- A malformed pip report is a nonzero manifest-projection failure, never a partial success.
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(entries, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
