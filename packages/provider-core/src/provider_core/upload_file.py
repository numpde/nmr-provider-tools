"""Verify immutable Upload file snapshots without API or HTTP dependencies."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import hashlib
import os
import stat
from builtins import BaseExceptionGroup
from dataclasses import dataclass, field


class UploadFailed(RuntimeError):
    """Safe reason with private response, cause, interruption and cleanup evidence.

    Cleanup failures are diagnostic observations, not ownership flags. They may
    remain available after settle_file confirms later abort cleanup.
    """

    # silent-defaults: allow-contract -- An omitted cleanup tuple carries no independent cleanup evidence; it does not confirm retirement.
    def __init__(self, reason, *, response=None, cause=None, cleanup_failures=(), interruption=None):
        super().__init__(f"Cannot acquire Upload: {reason}")
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
        super().__init__(f"Cannot read verified Upload file: {reason}")
        self.cause = cause
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption


@dataclass(frozen=True, slots=True)
class VerifiedUpload:
    job_ref: str
    upload_ref: str
    byte_length: int
    content_hash: str
    data: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.data) is not bytes or len(self.data) != self.byte_length:
            raise UploadFailed("source byte length differs from its transfer receipt")
        if "sha256:" + hashlib.sha256(self.data).hexdigest() != self.content_hash:
            raise UploadFailed("source SHA-256 differs from its transfer receipt")


@dataclass(frozen=True, slots=True)
class VerifiedUploadFile:
    """Verified transfer into a closed private file, owned by composition.

    name is relative to the directory borrowed for acquisition. A reader must
    verify its own immutable snapshot against these facts before parsing; this
    receipt cannot establish future pathname contents or durable storage.
    """

    job_ref: str
    upload_ref: str
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
    if type(receipt) is not VerifiedUploadFile:
        raise UploadFileReadFailed("a verified-file receipt is required before reading source bytes")
    if type(directory_fd) is not int or directory_fd < 0:
        raise UploadFileReadFailed("reading a private source requires an explicit open directory descriptor")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise UploadFileReadFailed("the reader's source byte ceiling must be a positive integer")
    if type(receipt.byte_length) is not int or not 0 <= receipt.byte_length <= maximum_bytes:
        raise UploadFileReadFailed("the receipt's source byte length is outside the reader's explicit ceiling")
    name = receipt.name
    if type(name) is not str or name in ("", ".", "..") or "/" in name or "\0" in name:
        raise UploadFileReadFailed("the private source locator must be one non-dot basename in the granted directory")
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except OSError as cause:
        raise UploadFileReadFailed("the private source file could not be opened", cause=cause) from None
    primary = None
    try:
        return _read_upload_snapshot(descriptor, receipt)
    except BaseException as cause:
        # This worker owns closure even when its synchronous read is interrupted.
        primary = cause
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as cause:
            cleanup = UploadFileReadFailed(
                "private source file closure is unconfirmed; retire its reader worker before releasing the source",
                cause=cause,
            )
            if type(primary) is UploadFileReadFailed:
                primary.cleanup_failures = (*primary.cleanup_failures, cleanup)
            elif isinstance(primary, asyncio.CancelledError):
                # Preserve the actual cancellation and its previous cause, as
                # JobUploads._link_file_interruption does for acquired-file I/O.
                retained = UploadFileReadFailed(
                    "source reading was interrupted before confirmed file closure",
                    cause=primary.__cause__,
                    cleanup_failures=(cleanup,),
                    interruption=primary,
                )
                primary.__cause__ = retained
            elif primary is not None:
                raise BaseExceptionGroup("Source reading and independent file closure failed", [primary, cleanup]) from None
            else:
                raise cleanup from None


def _read_upload_snapshot(descriptor, receipt):
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise UploadFileReadFailed("the opened private source is not a regular file")
        if details.st_size != receipt.byte_length:
            raise UploadFileReadFailed("the private source file byte length differs from its transfer receipt")
        chunks = []
        received = 0
        while chunk := os.read(descriptor, min(65536, receipt.byte_length + 1 - received)):
            chunks.append(chunk)
            received += len(chunk)
            if received > receipt.byte_length:
                break
        data = b"".join(chunks)
        try:
            return VerifiedUpload(receipt.job_ref, receipt.upload_ref, receipt.byte_length, receipt.content_hash, data)
        except UploadFailed as refusal:
            # Reuse the byte verifier, while reporting this reader's operation.
            # Its pure length/hash refusal has no grant or I/O evidence to copy.
            rejected = UploadFileReadFailed(refusal.reason)
        # Outside the handler, there is no synthetic acquisition context for
        # protected renderers to misreport as a failed grant operation.
        raise rejected
    except OSError as cause:
        raise UploadFileReadFailed("the private source file could not be read", cause=cause) from None


