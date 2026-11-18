"""Open a digest-verified file for a consumer to read through the same handle."""

from provider_core.container_guard import require_container

require_container()

import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import file_digest
from typing import BinaryIO


@contextmanager
def open_verified_file(*, source_file_path: pathlib.Path, expected_sha256: str) -> Iterator[BinaryIO]:
    """Verify and rewind a file, then keep it open for the consumer's context.

    The file must remain unchanged through verification and consumption.
    Keeping it open does not prevent another process from modifying its bytes.
    """

    with source_file_path.open(mode="rb") as source_file:
        actual_sha256 = file_digest(source_file, "sha256").hexdigest()

        if actual_sha256 != expected_sha256:
            raise ValueError(f"Cannot verify {source_file_path=}: {actual_sha256=} differs from {expected_sha256=}")

        source_file.seek(0)

        yield source_file
