"""Own one Job's inspected sources, fresh grants and bounded verified transfers.

Sources: the API revision in README, upload_transfer_http.py::_read_response_start
and upload_transfer_http_contract.py; SECS 34f5915c1534278bbf61b4c6592afa44cf94ab08,
src/secs_inference/provider/{job_api,upload_download}.py establish the operation
ordering precedent. No sibling implementation is imported. Membership observation
is not an atomic freeze: changes after the final refresh cannot be excluded.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
import hashlib
import math
import os
import secrets
import stat
import time
from builtins import BaseExceptionGroup
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from provider_core.transport import ExchangeFailed
from provider_core.upload_response import require_job_ref, require_upload_ref


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
class UploadLimits:
    """Explicit policy for one source session; repeated calls do not reset it."""

    per_source_bytes: int
    total_bytes: int
    requests: int
    seconds: float

    def __post_init__(self):
        for value in (self.per_source_bytes, self.total_bytes, self.requests):
            if not (type(value) is int and 0 < value):
                raise ValueError("Cannot configure Upload acquisition: byte and request limits must be positive integers")
        if not (type(self.seconds) in (int, float) and math.isfinite(self.seconds) and 0 < self.seconds):
            raise ValueError("Cannot configure Upload acquisition: duration must be finite positive seconds")


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


def require_store_origin(value):
    """Require an explicit canonical origin before any bearer can be attached."""
    if type(value) is not str or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("Cannot configure Upload store: origin must be visible ASCII HTTPS text")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("Cannot configure Upload store: origin is not a valid HTTPS URL") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.raw_path != b"/"
        or str(url) != value
        or value.endswith("/")
    ):
        raise ValueError(
            "Cannot configure Upload store: supply a canonical HTTPS origin without credentials, path, query or fragment"
        )
    return value


@dataclass(slots=True, repr=False)
class _FileIO:
    operation: str
    worker: object
    waiter: asyncio.Future


def _observe_file_io(waiter):
    if not waiter.cancelled():
        waiter.exception()


class _UploadFile:
    """Own one private leaf and its original create/close/remove operations.

    The caller lends an exclusively controlled private directory until handoff
    or cleanup; cancelled waits retain their original filesystem operations.
    Entry identity protects cleanup only; grant/hash facts own source admission.
    """

    def __init__(self, directory_fd, retirement_seconds):
        self.directory_fd = directory_fd
        self.retirement_seconds = retirement_seconds
        self.name = "upload-" + secrets.token_hex(16)
        self.fd = None
        self.created = False
        self.identity = None
        self.close_uncertain = False
        self.pending = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-upload-file")

    def _create(self):
        directory = os.fstat(self.directory_fd)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise UploadFailed("private-file destination must be an owner-controlled mode-0700 directory")
        self.fd = os.open(
            self.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        self.created = True
        metadata = os.fstat(self.fd)
        self.identity = metadata.st_dev, metadata.st_ino
        os.fchmod(self.fd, 0o600)

    def _close(self):
        descriptor, self.fd = self.fd, None
        try:
            os.close(descriptor)
        except OSError:
            # The kernel may have retired and reused the number despite failure.
            self.close_uncertain = True
            raise

    def _remove(self):
        try:
            metadata = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            self.created = False
            return
        if self.identity is None:
            raise UploadFailed(
                "original private-file identity could not be confirmed; preserve the entry and inspect the source directory"
            )
        if (metadata.st_dev, metadata.st_ino) != self.identity:
            raise UploadFailed("private source file entry changed; preserve the replacement and inspect the source directory")
        os.unlink(self.name, dir_fd=self.directory_fd)
        self.created = False

    async def create(self, seconds):
        await self._run("create", self._create, seconds)

    async def close(self, seconds):
        await self._run("close", self._close, seconds)

    async def remove(self, seconds):
        await self._run("remove", self._remove, seconds)

    async def _run(self, operation, function, seconds):
        worker = self._executor.submit(function)
        waiter = asyncio.wrap_future(worker)
        waiter.add_done_callback(_observe_file_io)
        self.pending = _FileIO(operation, worker, waiter)
        await self.settle(seconds)

    async def settle(self, seconds):
        pending = self.pending
        done, _unfinished = await asyncio.wait({pending.waiter}, timeout=seconds)
        if not done:
            raise UploadFailed(
                f"waiting to {pending.operation} the private file exceeded its allotted time",
                cause=TimeoutError("Private Upload file observation deadline expired"),
            )
        self.pending = None
        try:
            pending.worker.result()
        except Exception as cause:
            raise UploadFailed(
                f"cannot {pending.operation} the private source file; inspect the protected cause", cause=cause
            ) from None

    def shutdown(self):
        self._executor.shutdown(wait=False)


class JobUploads:
    """One serial source session, with no byte cache or implicit grant retry.

    Composition supplies separate API/store transport authority and owns retirement.
    Successful calls return immutable bytes or a closed private-file receipt.
    Partial or unverified transfers never become consumer results.
    Metadata responses have their API route ceilings; the byte budget counts raw
    store response bodies, including failures. The request/deadline limits cover
    membership, issuance and store calls together, starting at construction.
    """

    def __init__(self, job_ref, *, api, store, store_origin, limits):
        require_job_ref(job_ref)
        if type(limits) is not UploadLimits:
            raise TypeError("Cannot configure Upload acquisition: explicit UploadLimits are required")
        self.job_ref = job_ref
        self._api = api
        self._store = store
        self._origin = require_store_origin(store_origin)
        self._limits = limits
        self._deadline = time.monotonic() + limits.seconds
        self._requests = 0
        self._received = 0
        self._snapshot = None
        self._active = False
        self._grant_issued = False
        self._file = None
        self.last_failure = None

    @property
    def received_bytes(self):
        return self._received

    @property
    def requests_started(self):
        return self._requests

    @asynccontextmanager
    async def _operation(self):
        if self._active:
            raise UploadFailed("another source operation is active")
        if self._file is not None:
            raise UploadFailed("a private source file remains owned; settle its original I/O and cleanup before more source work")
        if not self._store.retirement_confirmed:
            raise UploadFailed("previous store resource retirement is unconfirmed; retire its transport before more work")
        self._active = True
        self._grant_issued = False
        self.last_failure = None
        try:
            yield
        except BaseException as failure:
            self._snapshot = None
            if self._grant_issued:
                # Successful issuance remains true even if later local checks,
                # transfer, or cancellation prevent a usable byte handoff.
                if isinstance(failure, UploadFailed):
                    evidence = failure
                elif isinstance(failure, asyncio.CancelledError) and isinstance(self.last_failure, UploadFailed):
                    evidence = self.last_failure
                else:
                    cause = self.last_failure if isinstance(failure, asyncio.CancelledError) else failure
                    evidence = UploadFailed("transfer did not complete after read access was issued", cause=cause)
                evidence.grant_issued = True
                evidence.add_note("Read access was issued before this failure and may have extended Upload retention.")
                self.last_failure = evidence
            elif not isinstance(failure, asyncio.CancelledError):
                self.last_failure = failure
            raise
        finally:
            self._active = False

    def _remaining_seconds(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise UploadFailed("the source session's total deadline expired")
        return remaining

    def _require_request_capacity(self):
        remaining = self._remaining_seconds()
        if self._requests >= self._limits.requests:
            raise UploadFailed(f"the source session exhausted its {self._limits.requests}-request limit")
        return remaining

    async def _call(self, resource_owner, operation, *args, **kwargs):
        remaining = self._require_request_capacity()
        self._requests += 1
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await operation(*args, **kwargs)
        except asyncio.CancelledError:
            self.last_failure = resource_owner.last_failure
            raise
        except TimeoutError:
            if not deadline.expired():
                raise
            raise UploadFailed("the source session's total deadline expired", cause=resource_owner.last_failure) from None

    async def inspect(self):
        """Refresh the source list; callers choose from this observation."""
        async with self._operation():
            self._snapshot = await self._call(self._api, self._api.uploads, self.job_ref)
            return self._snapshot

    async def acquire(self, upload_ref):
        """Require unchanged Job membership, obtain access, and verify source bytes.

        Re-read the inspected membership before requesting a fresh read grant;
        reject changes rather than silently selecting from the new list.
        """
        async with self._operation():
            selected = self._select(upload_ref)
            grant = await self._grant(selected)
            return await self._download(grant)

    def _select(self, upload_ref):
        require_upload_ref(upload_ref)
        self._require_request_capacity()
        if self._snapshot is None:
            raise UploadInputRejected("inspect the current Job Uploads before selecting a source")
        selected = next((item for item in self._snapshot.members if item.upload_ref == upload_ref), None)
        if selected is None:
            raise UploadInputRejected("selected Upload is not in the inspected Job membership; inspect and select again")
        # Correction cannot revive spent work. Neither refusal issues a grant.
        if selected.byte_length > self._limits.total_bytes - self._received:
            raise UploadFailed("selected Upload exceeds the source session's remaining byte budget")
        if selected.byte_length > self._limits.per_source_bytes:
            raise UploadInputRejected(
                f"selected Upload has {selected.byte_length} bytes and exceeds the fixed "
                f"{self._limits.per_source_bytes}-byte per-source limit; select a smaller Upload"
            )
        return selected

    async def _grant(self, selected):
        refreshed = await self._call(self._api, self._api.uploads, self.job_ref)
        if refreshed != self._snapshot:
            self._require_request_capacity()
            raise UploadInputRejected("Job Upload membership changed; inspect the current list and select again")
        grant = await self._call(self._api, self._api.read_grant, self.job_ref, selected.upload_ref)
        self._grant_issued = True
        if (
            grant.job_ref != self.job_ref
            or grant.upload_ref != selected.upload_ref
            or grant.byte_length != selected.byte_length
            or (selected.content_hash is not None and grant.content_hash != selected.content_hash)
        ):
            raise UploadFailed("download grant differs from the selected Upload identity or byte facts")
        return grant

    async def acquire_file(self, upload_ref, *, directory_fd, retirement_seconds):
        """Verify one transfer into a closed private file; lend its directory.

        Keep directory_fd open and exclusively controlled until success or
        settle_file confirms failed-file cleanup. A successful receipt transfers
        file lifetime to composition, which must wait for readers before deletion.
        """
        async with self._operation():
            selected = self._select(upload_ref)
            if type(directory_fd) is not int or directory_fd < 0:
                raise UploadFailed("private-file acquisition requires an open directory descriptor")
            if type(retirement_seconds) not in (int, float) or not math.isfinite(retirement_seconds) or retirement_seconds <= 0:
                raise UploadFailed("private-file cleanup requires explicit finite positive retirement seconds")
            file = self._file = _UploadFile(directory_fd, retirement_seconds)
            try:
                await file.create(self._remaining_seconds())
                grant = await self._grant(selected)
                response = await self._transfer(grant, output_fd=file.fd)
                _verify_store_headers(response, grant)
                if response.byte_length != grant.byte_length:
                    raise UploadFailed("downloaded byte length differs from the grant")
                if response.content_hash != grant.content_hash:
                    raise UploadFailed("downloaded SHA-256 differs from the grant")
                await file.close(self._remaining_seconds())
                self._remaining_seconds()
                result = VerifiedUploadFile(self.job_ref, grant.upload_ref, grant.byte_length, grant.content_hash, file.name)
                file.shutdown()
                self._file = None
                return result
            except BaseException as failure:
                self._retain_file_failure(failure)
                if self._store.retirement_confirmed:
                    try:
                        await self._abort_file()
                    except asyncio.CancelledError as interruption:
                        if isinstance(failure, asyncio.CancelledError):
                            self._link_file_interruption(failure)
                            raise failure  # noqa: B904 -- Preserve the original cancellation and its owned cause; this later cancellation remains its context.
                        self._link_file_interruption(interruption)
                        raise
                    except UploadFailed as cleanup:
                        self._retain_file_cleanup(cleanup)
                if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    if isinstance(failure, asyncio.CancelledError):
                        self._link_file_interruption(failure)
                    raise
                raise self.last_failure from None

    def _retain_file_failure(self, failure):
        if isinstance(failure, UploadFailed):
            self.last_failure = failure
        else:
            cause = (
                self.last_failure if isinstance(failure, asyncio.CancelledError) and self.last_failure is not None else failure
            )
            self.last_failure = UploadFailed("private-file acquisition did not complete", cause=cause)

    def _remember_file_interruption(self, cause):
        retained = self.last_failure.interruption
        if retained is None:
            self.last_failure.interruption = cause
        elif cause is not None and cause is not retained:
            self.last_failure.interruption = BaseExceptionGroup("Upload interruption observations", (retained, cause))

    def _link_file_interruption(self, interruption):
        # Match AttemptExecutor._link_interruption: keep the canonical record
        # reachable from the actual cancellation, without erasing its old cause.
        if interruption.__cause__ is not self.last_failure:
            self._remember_file_interruption(interruption.__cause__)
            interruption.__cause__ = self.last_failure

    def _retain_file_cleanup(self, failure):
        if isinstance(self.last_failure, UploadInputRejected):
            self.last_failure = UploadFailed(
                "input selection was rejected and private-file cleanup remains unresolved", cause=self.last_failure
            )
        # A pending wait supplies no new filesystem result. Keep the concrete
        # cleanup cause already retained, without accumulating polling history.
        if not self.last_failure.cleanup_failures or self._file.pending is None:
            self.last_failure.cleanup_failures = (failure,)
        if self._file.close_uncertain and self._file.pending is None:
            self._file.shutdown()

    async def _abort_file(self):
        file = self._file
        deadline = time.monotonic() + file.retirement_seconds

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise UploadFailed("private-file cleanup observation expired; preserve its original I/O and file")
            return seconds

        if file.pending is not None:
            operation = file.pending.operation
            try:
                await file.settle(remaining())
            except UploadFailed as failure:
                if file.pending is not None or operation != "create":
                    raise
                # No grant can precede an observed create. Its late intrinsic
                # failure replaces only that create wait's interruption cause.
                self._remember_file_interruption(self.last_failure.cause)
                self.last_failure.cause = failure
        if file.close_uncertain:
            raise UploadFailed("private-file descriptor closure is unconfirmed; retire the enclosing process")
        if file.fd is not None:
            await file.close(remaining())
        if file.created:
            await file.remove(remaining())
        file.shutdown()
        self._file = None

    async def settle_file(self):
        """Confirm abort cleanup only, without resuming a grant or download.

        Composition must first retire the exact store exchange with this
        JobUploads as owner. The retained cleanup grace works after the source
        deadline expires; source request/byte/deadline budgets never reset.
        """
        if self._active:
            raise UploadFailed("another source operation is active")
        if self._file is None:
            raise UploadFailed("no unresolved private source file is available to settle")
        if not self._store.retirement_confirmed:
            raise UploadFailed(
                "store retirement remains unconfirmed; retire the exact transfer before settling its file",
                cause=self.last_failure,
            )
        if self._file.close_uncertain and self._file.pending is None:
            raise UploadFailed(
                "private-file descriptor closure is unconfirmed; retire the enclosing process", cause=self.last_failure
            )
        self._active = True
        try:
            await self._abort_file()
        except asyncio.CancelledError as interruption:
            self._link_file_interruption(interruption)
            raise
        except UploadFailed as failure:
            self._retain_file_cleanup(failure)
            raise UploadFailed(
                "private-file cleanup remains unresolved; preserve its file and original I/O", cause=self.last_failure
            ) from None
        finally:
            self._active = False

    def _account_bytes(self, count):
        # Count the offending chunk too: bytes already consumed cannot be refunded.
        self._received += count
        if self._received > self._limits.total_bytes:
            raise UploadFailed(f"download exhausted the source session's {self._limits.total_bytes}-byte budget")

    async def _download(self, grant):
        response = await self._transfer(grant)
        _verify_store_headers(response, grant)
        verified = VerifiedUpload(self.job_ref, grant.upload_ref, grant.byte_length, grant.content_hash, response.body)
        self._remaining_seconds()
        return verified

    def _download_request(self, grant):
        target = self._origin + "/upload/v1/uploads/" + grant.upload_ref + "/bytes"
        if grant.download_url != target:
            raise UploadFailed("download grant does not name the configured store and exact Upload path")
        if grant.expires_at <= datetime.now(UTC):
            raise UploadFailed(
                "download grant expired before transfer; inspect sources before explicitly requesting fresh access"
            )
        if not grant.capability or any(ord(c) <= 32 or ord(c) >= 127 for c in grant.capability):
            raise UploadFailed("download bearer cannot be represented as a single HTTP authorization value")
        request = httpx.Request("GET", target, headers={"Authorization": "Bearer " + grant.capability})
        if str(request.url) != target:
            raise UploadFailed("HTTP client rewrote the admitted store target")
        return request

    async def _transfer(self, grant, *, output_fd=None):
        request = self._download_request(grant)
        try:
            if output_fd is None:
                return await self._call(
                    self._store, self._store.exchange, request, maximum_bytes=grant.byte_length, account_bytes=self._account_bytes
                )
            return await self._call(
                self._store,
                self._store.exchange_file,
                request,
                output_fd=output_fd,
                maximum_bytes=grant.byte_length,
                account_bytes=self._account_bytes,
                owner=self,
            )
        except ExchangeFailed as cause:
            reason = str(cause.primary) if isinstance(cause.primary, UploadFailed) else str(cause)
            raise UploadFailed(reason, cause=cause) from None


def _verify_store_headers(response, grant):
    if response.status != 200:
        raise UploadFailed(f"store returned HTTP {response.status}; no verified bytes were obtained", response=response)
    digest = base64.b64encode(bytes.fromhex(grant.content_hash.removeprefix("sha256:")))
    for name, expected in (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(grant.byte_length).encode("ascii")),
        (b"content-digest", b"sha-256=:" + digest + b":"),
    ):
        if [value for key, value in response.headers if key.lower() == name] != [expected]:
            raise UploadFailed(f"store response {name.decode()} does not match the granted whole-file transfer", response=response)
    if any(name.lower() in {b"content-encoding", b"content-range"} for name, _ in response.headers):
        raise UploadFailed("encoded or partial store responses are not admitted", response=response)
