"""Project an owned offline pip report into shared stream inputs."""

import argparse
import json
import sys

from wheelhouse_tool.validation.selection.pip_report import selected_artifacts


def manifest(report, pool, directory, allowed_sources):
    """Sort admitted artifacts for a successful hash-checked pip run."""

    entries = selected_artifacts(report, pool, directory, allowed_sources)

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
