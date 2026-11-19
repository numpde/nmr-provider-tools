"""Identify the explicit selected artifacts of a build context."""

import argparse
import hashlib
import json
import sys

from wheelhouse_tool.validation.selection.identity import selected_artifacts


def selection_identity(entries):
    """Hash admitted artifacts independently of entry order and identical repeats."""

    selected = selected_artifacts(entries)

    canonical = [
        {"directory": directory, "filename": name, "sha256": checksum}
        for ((directory, name), checksum) in sorted(selected.items())
    ]

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
