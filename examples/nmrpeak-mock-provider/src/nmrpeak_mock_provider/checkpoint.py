"""Verify and load through the same descriptor, following NMRPeak's loader.

Deployment supplies an installed checkpoint that it does not overwrite while
the host is alive. This does not provide protection against in-place writers.
"""

from hashlib import sha256
from pathlib import Path
from typing import BinaryIO


def open_checkpoint(*, path: Path, expected_sha256: str) -> BinaryIO:
    checkpoint = path.open(mode="rb")
    try:
        measured = sha256()
        while block := checkpoint.read(1024 * 1024):
            measured.update(block)
        actual_sha256 = measured.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(f"Cannot load checkpoint: {actual_sha256=} differs from {expected_sha256=}")
        checkpoint.seek(0)
        return checkpoint
    except BaseException:
        checkpoint.close()
        raise
