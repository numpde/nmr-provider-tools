"""Verify immutable Upload file snapshots without API or HTTP dependencies."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import hashlib
import os
import reprlib
import stat
from builtins import BaseExceptionGroup
from dataclasses import dataclass, field

from provider_core.type_requirements import require_type


class UploadFailed(RuntimeError):
    """Safe reason with private response, cause, interruption and cleanup evidence.

    Cleanup failures are diagnostic observations, not ownership flags. They may
    remain available after settle_file confirms later abort cleanup.
    """

    # silent-defaults: allow-contract -- An omitted cleanup tuple carries no independent cleanup evidence; it does not confirm retirement.
    def __init__(self, reason, *, response=None, cause=None, cleanup_failures=(), interruption=None):
        super().__init__(f"Cannot acquire Upload: {reason = }")
        self.reason = reason
        self.response = response
        self.cause = cause
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption
        self.grant_issued = False


class UploadInputRejected(UploadFailed):
    """A source selection or fixed per-file input limit permits correction.

    Grant, transfer, budget and retirement failures remain operational. Only the
    acquisition owner distinguishes these pre-grant refusals from spent budgets.
    """


class UploadFileReadFailed(RuntimeError):
    """A reader's operational failure, separate from grant acquisition facts."""

    # silent-defaults: allow-contract -- An omitted cleanup tuple carries no independent cleanup evidence; it does not confirm retirement.
    def __init__(self, reason, *, cause=None, cleanup_failures=(), interruption=None):
        super().__init__(f"Cannot read verified Upload file: {reason = }")
        self.cause = cause
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption


@dataclass(frozen=True, slots=True)
class VerifiedUpload:
    job_ref: str  # repo-check: allow-identifier-type -- Transfer receipt retains the opaque Job identity checked by UploadTransfer.
    upload_ref: str  # repo-check: allow-identifier-type -- Transfer receipt retains the opaque Upload identity checked by require_read_grant.
    byte_length: int
    content_hash: str
    data: bytes = field(repr=False)

    def __post_init__(self):
        require_type(self.data, bytes, error_factory=UploadFailed)

        if not (len(self.data) == self.byte_length):
            raise UploadFailed(f"{len(self.data)=} ≠ {self.byte_length=}")

        data_content_hash = "sha256:" + hashlib.sha256(self.data).hexdigest()

        if not (data_content_hash == self.content_hash):
            raise UploadFailed(f"{data_content_hash=} ≠ {self.content_hash=}")


@dataclass(frozen=True, slots=True)
class VerifiedUploadFile:
    """Verified transfer into a closed private file, owned by composition.

    name is relative to the directory borrowed for acquisition. A reader must
    verify its own immutable snapshot against these facts before parsing; this
    receipt cannot establish future pathname contents or durable storage.
    """

    job_ref: str  # repo-check: allow-identifier-type -- File receipt retains the opaque Job identity checked by UploadTransfer.
    upload_ref: str  # repo-check: allow-identifier-type -- File receipt retains the opaque Upload identity checked by require_read_grant.
    byte_length: int
    content_hash: str
    name: str = field(repr=False)


def read_upload_file(receipt, *, directory_fd, maximum_bytes):
    """Verify immutable source bytes inside an already supervised reader worker.

    SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    source_access.py::_read_stream and input_adapter.py::_verify_components
    establish the immutable-consumption seam. A checked path or an earlier hash
    cannot establish bytes later read from a mutable file. Open once, verify the
    copied bytes, close once, then let the consumer parse only the returned data.
    Composition retains the source until reader quiescence or confirmed stop.
    """

    require_type(receipt, VerifiedUploadFile, error_factory=UploadFileReadFailed)

    require_type(directory_fd, int, error_factory=UploadFileReadFailed)

    if not (directory_fd >= 0):
        raise UploadFileReadFailed(f"{directory_fd=} should be nonnegative")

    require_type(maximum_bytes, int, error_factory=UploadFileReadFailed)

    if not (maximum_bytes > 0):
        raise UploadFileReadFailed(f"{maximum_bytes=} should be positive")

    require_type(receipt.byte_length, int, error_factory=UploadFileReadFailed)

    if not (0 <= receipt.byte_length <= maximum_bytes):
        raise UploadFileReadFailed(f"{receipt.byte_length=} should be nonnegative and not exceed {maximum_bytes=}")

    source_basename = receipt.name

    require_type(source_basename, str, error_factory=UploadFileReadFailed)

    if not (source_basename not in (forbidden_basenames := ("", ".", ".."))):
        raise UploadFileReadFailed(f"{reprlib.repr(source_basename)=} should not be one of {forbidden_basenames=}")

    if not ("/" not in source_basename):
        raise UploadFileReadFailed(f"{reprlib.repr(source_basename)=} should not contain '/'")

    if not ("\0" not in source_basename):
        raise UploadFileReadFailed(f"{reprlib.repr(source_basename)=} should not contain NUL characters")

    try:
        upload_source_fd = os.open(source_basename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except OSError as cause:
        raise UploadFileReadFailed(f"Cannot open {reprlib.repr(source_basename)=} in {directory_fd=}", cause=cause) from None

    read_failure = None

    try:
        return _read_upload_snapshot(upload_source_fd, receipt)
    except BaseException as cause:
        # This worker owns closure even when its synchronous read is interrupted.
        read_failure = cause

        raise
    finally:
        try:
            os.close(upload_source_fd)
        except OSError as cause:
            close_failure = UploadFileReadFailed(
                f"Cannot confirm closure of {upload_source_fd=}; retire its reader before releasing the source",
                cause=cause,
            )

            if type(read_failure) is UploadFileReadFailed:
                read_failure.cleanup_failures = (*read_failure.cleanup_failures, close_failure)
            elif isinstance(read_failure, asyncio.CancelledError):
                # Preserve the actual cancellation and its previous cause,
                # as JobUploads._link_file_interruption does for acquired-file I/O.
                retained = UploadFileReadFailed(
                    f"Reading {upload_source_fd=} was interrupted before confirmed closure",
                    cause=read_failure.__cause__,
                    cleanup_failures=(close_failure,),
                    interruption=read_failure,
                )

                read_failure.__cause__ = retained
            elif not (read_failure is None):
                raise BaseExceptionGroup(f"Reading and closing {upload_source_fd=} failed independently", [read_failure, close_failure]) from None
            else:
                raise close_failure from None


def _read_upload_snapshot(upload_source_fd, receipt):
    try:
        upload_source_stat = os.fstat(upload_source_fd)

        if not stat.S_ISREG(file_mode := upload_source_stat.st_mode):
            raise UploadFileReadFailed(f"{upload_source_fd=} has bad {file_mode=}")

        if not (upload_source_stat.st_size == receipt.byte_length):
            raise UploadFileReadFailed(f"{upload_source_stat.st_size=} ≠ {receipt.byte_length=}")

        chunks = []
        received = 0

        while chunk := os.read(upload_source_fd, min(65536, receipt.byte_length + 1 - received)):
            chunks.append(chunk)
            received += len(chunk)

            if received > receipt.byte_length:
                break

        source_bytes = b"".join(chunks)

        try:
            return VerifiedUpload(receipt.job_ref, receipt.upload_ref, receipt.byte_length, receipt.content_hash, source_bytes)
        except UploadFailed as refusal:
            # Reuse the byte verifier, while reporting this reader's operation.
            # Its pure length/hash refusal has no grant or I/O evidence to copy.
            rejected = UploadFileReadFailed(refusal.reason)

        # Outside the handler, there is no synthetic acquisition context for
        # protected renderers to misreport as a failed grant operation.
        raise rejected
    except OSError as cause:
        raise UploadFileReadFailed(f"Cannot read {upload_source_fd=}", cause=cause) from None


