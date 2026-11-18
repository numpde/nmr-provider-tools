"""Import captured acquisition modules using only their extracted source tree."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main(root: Path) -> None:
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Cold import check requires an extracted absolute source directory")

    sys.path.insert(0, str(root))
    importlib.import_module("linux_idmap.probe")
    importlib.import_module("provider_core.bound_http_proxy")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Supply one extracted acquisition source directory")
    main(Path(sys.argv[1]))
