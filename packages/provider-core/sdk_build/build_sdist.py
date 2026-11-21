"""Build the staged source distribution with the pinned setuptools backend."""

from __future__ import annotations

import argparse
import reprlib
from pathlib import Path

from setuptools.build_meta import build_sdist


def main(destination: Path) -> None:
    if (not destination.is_absolute()) or (not destination.is_dir()):
        raise ValueError(f"{reprlib.repr(destination)=} should be an existing absolute output directory")

    build_sdist(str(destination))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()

    main(arguments.destination)
