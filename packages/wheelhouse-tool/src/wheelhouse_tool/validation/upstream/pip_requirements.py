"""Read the pip requirements syntax used by the standalone tool's flat locks."""

import re
import reprlib
from collections.abc import Iterator


def requirement_lines(text: str) -> Iterator[str]:
    """Yield nonempty lines with any final continuation character removed."""

    # https://pip.pypa.io/en/stable/reference/requirements-file-format/
    for raw in text.splitlines():
        line = raw.strip().removesuffix("\\").rstrip()

        if (line) and (not line.startswith("#")):
            yield line


def index_url_option(line: str) -> str | None:
    """Read the value of an index directive in the supported syntax."""

    if line.startswith("--index-url "):
        return line.split(None, 1)[1]

    return None


def sha256_option(line: str) -> str | None:
    """Read a SHA-256 hash option without checking its value or placement."""

    prefix = "--hash=sha256:"

    if line.startswith(prefix):
        return line.removeprefix(prefix)

    return None


def require_sha256(checksum: str) -> None:
    """Require the lowercase hexadecimal digest spelling used by the lock."""

    if not re.fullmatch((expected_pattern := r"[0-9a-f]{64}"), checksum):
        raise ValueError(f"{reprlib.repr(checksum)=} should match {expected_pattern=}")
