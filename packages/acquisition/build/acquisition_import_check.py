"""Import captured acquisition modules using only their extracted source tree."""

from __future__ import annotations

import argparse
import importlib
import reprlib
import sys
from pathlib import Path


def main(root: Path) -> None:
    if (not root.is_absolute()) or (not root.is_dir()):
        raise ValueError(f"{reprlib.repr(root)=} should be an existing absolute source directory")

    sys.path.insert(0, str(root))
    importlib.import_module("linux_idmap.probe")
    importlib.import_module("provider_core.bound_http_proxy")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("source_directory", type=Path)
    arguments = parser.parse_args()

    main(arguments.source_directory)
