"""Explain rejected artifact metadata differences without repeating admission."""

from tools.container_guard import require_container

require_container()

import hashlib
import reprlib


# Preserve ordinary artifact names and digests while bounding malformed metadata
# repo-check: allow-module-constant
_METADATA_EVIDENCE = reprlib.Repr()

_METADATA_EVIDENCE.maxstring = 256
_METADATA_EVIDENCE.maxother = _METADATA_EVIDENCE.maxstring


def metadata_mismatch(recorded_metadata: object, captured_metadata: object) -> str:
    """Describe an already rejected difference, identifying its first field."""

    # Type dispatch selects evidence after the caller's existing mismatch check
    if (
        isinstance(recorded_metadata, dict) and
        isinstance(captured_metadata, dict)
    ):
        for metadata_member in sorted(recorded_metadata.keys() | captured_metadata.keys(), key=repr):
            if metadata_member not in recorded_metadata:
                return f"{_METADATA_EVIDENCE.repr(metadata_member)=} should be present in recorded metadata"

            if metadata_member not in captured_metadata:
                return f"{_METADATA_EVIDENCE.repr(metadata_member)=} should be absent from recorded metadata"

            if recorded_metadata[metadata_member] != captured_metadata[metadata_member]:
                return f"{_METADATA_EVIDENCE.repr(metadata_member)=}: " + metadata_mismatch(
                    recorded_metadata=recorded_metadata[metadata_member],
                    captured_metadata=captured_metadata[metadata_member],
                )

    if (
        isinstance(recorded_metadata, set) and
        isinstance(captured_metadata, set)
    ):
        return (
            f"{_METADATA_EVIDENCE.repr(recorded_metadata - captured_metadata)=} and "
            f"{_METADATA_EVIDENCE.repr(captured_metadata - recorded_metadata)=} should be empty"
        )

    if (
        isinstance(recorded_metadata, bytes) and
        isinstance(captured_metadata, bytes)
    ):
        recorded_metadata_sha256 = hashlib.sha256(recorded_metadata).hexdigest()
        captured_metadata_sha256 = hashlib.sha256(captured_metadata).hexdigest()

        return f"{recorded_metadata_sha256=} ≠ {captured_metadata_sha256=}"

    return f"{_METADATA_EVIDENCE.repr(recorded_metadata)=} ≠ {_METADATA_EVIDENCE.repr(captured_metadata)=}"
