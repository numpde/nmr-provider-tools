"""Build the staged source distribution with the pinned setuptools backend."""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools.build_meta import build_sdist


def main(destination: Path) -> None:
    if not destination.is_absolute() or not destination.is_dir():
        raise ValueError("Source distribution output must be an existing absolute directory")
    build_sdist(str(destination))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Supply one source distribution output directory")
    main(Path(sys.argv[1]))
