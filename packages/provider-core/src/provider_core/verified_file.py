"""Open a digest-verified file for a consumer to read through the same handle."""

from provider_core.container_guard import require_container

require_container()

from hashlib import file_digest
from pathlib import Path
from typing import BinaryIO


def open_verified_file(*, source_file_path: Path, expected_sha256: str) -> BinaryIO:
    """Verify and rewind a file; the caller owns closing the returned handle.

    The file must remain unchanged through verification and consumption.
    Keeping it open does not prevent another process from modifying its bytes.
    """
    source_file = source_file_path.open(mode="rb")

    try:
        actual_sha256 = file_digest(source_file, "sha256").hexdigest()

        if actual_sha256 != expected_sha256:
            raise ValueError(f"Cannot verify {source_file_path=}: {actual_sha256=} differs from {expected_sha256=}")

        source_file.seek(0)

        return source_file
    except BaseException:
        source_file.close()
        raise
