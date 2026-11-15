"""Verify checkpoint bytes and let the model load through the same open file.

Provider assembly supplies the path; this example uses external JSON model-data substitutes.
Whoever maintains that file must leave its contents unchanged during verification
and loading. Holding it open does not prevent another process from rewriting it.
"""

from hashlib import sha256
from pathlib import Path
from typing import BinaryIO


def open_checkpoint(*, path: Path, expected_sha256: str) -> BinaryIO:
    """Return the verified file at byte zero; the caller must close it after loading."""
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
