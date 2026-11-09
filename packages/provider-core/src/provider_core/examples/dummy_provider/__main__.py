"""Execute fake jobs using the installed shared scientific interface."""

from provider_core.container_guard import require_container

require_container()

import argparse
import asyncio
import sys
import traceback

from .demo import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", type=int, choices=(1, 2), required=True,
                        help="Number of independent copies of the same scientific implementation")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.instances))
    except BaseException:
        traceback.print_exc()
        print("DUMMY PROVIDER FAILED: see the original exception above; no successful completion claimed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
