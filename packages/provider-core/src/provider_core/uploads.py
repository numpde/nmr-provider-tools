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
import math
import os
import secrets
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpcore
import httpx

from provider_core.api import ApiFailure
from provider_core.task_boundary import contain_task_exit
from provider_core.transport import ExchangeFailed, temporary_io_failure
from provider_core.response_json import ResponseRejected
from provider_core.upload_response import require_job_ref, require_read_grant, require_upload_ref


from provider_core.upload_file import (
    UploadFailed, UploadFileReadFailed, UploadInputRejected,
    VerifiedUpload, VerifiedUploadFile, read_upload_file,
)


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


class UploadRecoveryExhausted(UploadFailed):
    """An eligible source outage exhausted opt-in recovery after exact retirement."""


@dataclass(frozen=True, slots=True)
class UploadRecovery:
    """Bound logical source attempts within the owner's original spent budgets.

    stopping is the scientific stop event, not admission stop. on_retry is a
    bounded synchronous protected observer accepting (failure, wait_seconds).
    Composition receives a verified result before reading recovery_failure for
    its protected recovery notice; no observer runs after file handoff here.
    """

    maximum_attempts: int
    initial_seconds: float
    maximum_seconds: float
    stopping: asyncio.Event = field(repr=False)
    on_retry: object = field(repr=False)

    def __post_init__(self):
        if not (type(self.maximum_attempts) is int and 0 < self.maximum_attempts):
            raise ValueError("Upload recovery requires a positive integer attempt limit including the initial attempt")
        for seconds in (self.initial_seconds, self.maximum_seconds):
            if not (type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds):
                raise ValueError("Upload recovery pacing requires finite positive seconds")
        if not (self.initial_seconds <= self.maximum_seconds):
            raise ValueError("Upload recovery initial wait cannot exceed its maximum wait")
        if not isinstance(self.stopping, asyncio.Event) or not callable(self.on_retry):
            raise TypeError("Upload recovery requires a scientific stop Event and protected synchronous retry observer")


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


class UploadTransfer:
    """Own verified store transfers and their original file and I/O cleanup.

    Composition supplies remaining time and synchronous request and byte
    accounting callbacks. This owner bounds the store exchange by the supplied
    remaining time and retains it until retirement. The async grant factory runs only after a
    private file was created; it owns selection and grant policy. No retries or
    membership reads are implicit. A supplied grant alone does not establish
    when or whether the caller obtained it from the API.
    """

    def __init__(self, job_ref, *, store, store_origin, remaining, before_request, account, exchange_owner=None):
        require_job_ref(job_ref)
        if not all(callable(value) for value in (remaining, before_request, account)):
            raise TypeError("Upload transfer requires explicit remaining-time, request and byte-accounting owners")
        self.job_ref, self._store = job_ref, store
        self._origin = require_store_origin(store_origin)
        self._remaining, self._before_request, self._account_bytes = remaining, before_request, account
        self._exchange_owner = self if exchange_owner is None else exchange_owner
        self._file = None
        self._active = False
        self._reset_failure()

    def _reset_failure(self):
        self.last_failure = None
        self._transfer_grant = None
        self._store_outcome = None
        self._header_failure = None
        self._interrupted = False

    def can_retry_transfer(self, failure):
        """Permit a fresh whole-file transfer after this exact settled failure.

        SECS 34f5915c analysis_run.py admits 401/503/507 refusals
        and temporary network failures; upload_download.py validates whole-file
        headers before reading the stream. Selection, grant renewal and bounded
        retry pacing remain with composition. A provided grant proves no issuance.
        """
        if (
            failure is not self.last_failure
            or not isinstance(failure, UploadFailed)
            or failure.interruption is not None
            or self._interrupted
            or self._active
            or not self._store.retirement_confirmed
            or self.file_cleanup_pending
            or self._transfer_grant is None
        ):
            return False
        outcome = self._store_outcome
        if isinstance(outcome, ExchangeFailed):
            if (failure.cause is not outcome or outcome is not self._store.last_failure
                    or outcome.cleanup_failures or outcome.interruption is not None):
                return False
            head = outcome.response_head
            if self._header_failure is not None and outcome.primary is self._header_failure:
                return (head is not None and self._header_failure.response is head
                        and head.status in {401, 503, 507})
            if outcome.primary is not outcome.network_failure:
                return False
            if head is not None:
                try:
                    _verify_store_headers(head, self._transfer_grant)
                # silent-defaults: allow-exception-outcome -- Unadmitted whole-file headers grant no retry; the original transfer failure remains retained.
                except UploadFailed:
                    return False
            if head is not None and any(name.lower() == b"transfer-encoding" for name, _ in head.headers):
                return False
            if type(outcome.primary) is httpcore.RemoteProtocolError:
                return head is not None
            return temporary_io_failure(outcome.primary, 8, network_failure=outcome.network_failure)
        return False

    @property
    def file_cleanup_pending(self):
        """Report whether this owner still retains an unresolved private file."""
        return self._file is not None

    def _remaining_seconds(self):
        seconds = self._remaining()
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise UploadFailed("source transfer requires a positive finite remaining deadline")
        return seconds

    @asynccontextmanager
    async def _operation(self):
        if self._active:
            raise UploadFailed("another source transfer is active")
        if self._file is not None:
            raise UploadFailed("a private source file remains owned; settle its original I/O and cleanup before more source work")
        if not self._store.retirement_confirmed:
            raise UploadFailed("previous store resource retirement is unconfirmed; retire its transport before more work")
        self._active = True
        self._reset_failure()
        try:
            yield
        finally:
            self._active = False

    async def acquire_file(self, grant_factory, *, directory_fd, retirement_seconds):
        """Verify one transfer into a closed private file; lend its directory.

        Keep directory_fd open and exclusively controlled until success or
        settle_file confirms failed-file cleanup. A successful receipt transfers
        file lifetime to composition, which must wait for readers before deletion.
        """
        async with self._operation():
            if not callable(grant_factory):
                raise TypeError("Upload transfer requires an async grant factory")
            if type(directory_fd) is not int or directory_fd < 0:
                raise UploadFailed("private-file acquisition requires an open directory descriptor")
            if type(retirement_seconds) not in (int, float) or not math.isfinite(retirement_seconds) or retirement_seconds <= 0:
                raise UploadFailed("private-file cleanup requires explicit finite positive retirement seconds")
            file = self._file = _UploadFile(directory_fd, retirement_seconds)
            try:
                await file.create(self._remaining_seconds())
                deadline = asyncio.timeout(self._remaining_seconds())
                try:
                    async with deadline:
                        grant = await grant_factory()
                except TimeoutError as failure:
                    if not deadline.expired():
                        raise
                    prior = self.last_failure
                    if prior is None:
                        raise UploadFailed(
                            "the source session's total deadline expired while obtaining the grant",
                            cause=failure,
                        ) from None
                    raise UploadFailed(
                        "the source session's total deadline expired while obtaining the grant",
                        cause=prior,
                        interruption=failure,
                    ) from None
                response = await self._transfer(grant, output_fd=file.fd)
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
                if isinstance(failure, asyncio.CancelledError):
                    self._interrupted = True
                self._retain_file_failure(failure)
                if self._store.retirement_confirmed:
                    try:
                        await self._abort_file()
                    except asyncio.CancelledError as interruption:
                        self._interrupted = True
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

        Composition must first retire the exact store exchange with the
        configured exchange owner. The retained cleanup grace works after the
        source deadline expires; acquisition budgets never reset.
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
            self._interrupted = True
            self._link_file_interruption(interruption)
            raise
        except UploadFailed as failure:
            self._retain_file_cleanup(failure)
            raise UploadFailed(
                "private-file cleanup remains unresolved; preserve its file and original I/O", cause=self.last_failure
            ) from None
        finally:
            self._active = False

    async def _download(self, grant):
        response = await self._transfer(grant)
        verified = VerifiedUpload(self.job_ref, grant.upload_ref, grant.byte_length, grant.content_hash, response.body)
        self._remaining_seconds()
        return verified

    def _download_request(self, grant):
        try:
            require_read_grant(grant, job_ref=self.job_ref)
        except ResponseRejected as failure:
            raise UploadFailed("the read grant is malformed or belongs to a different Job", cause=failure) from None
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
        self._before_request()
        deadline = asyncio.timeout(self._remaining_seconds())
        self._transfer_grant = grant
        def admit(head):
            try:
                _verify_store_headers(head, grant)
            except UploadFailed as failure:
                self._header_failure = failure
                raise
        try:
            async with deadline:
                if output_fd is None:
                    response = await self._store.exchange(
                        request, maximum_bytes=grant.byte_length, account_bytes=self._account_bytes, before_body=admit,
                    )
                else:
                    response = await self._store.exchange_file(
                        request, output_fd=output_fd, maximum_bytes=grant.byte_length,
                        account_bytes=self._account_bytes, owner=self._exchange_owner, before_body=admit,
                    )
                self._store_outcome = response
                return response
        except TimeoutError:
            if not deadline.expired():
                raise
            raise UploadFailed("the source session's total deadline expired", cause=self._store.last_failure) from None
        except asyncio.CancelledError:
            self._interrupted = True
            self._store_outcome = self._store.last_failure
            if self.last_failure is None and self._store.last_failure is not None:
                self.last_failure = self._store.last_failure
            raise
        except ExchangeFailed as cause:
            self._store_outcome = cause
            reason = str(cause.primary) if isinstance(cause.primary, UploadFailed) else str(cause)
            raise UploadFailed(reason, cause=cause) from None


class JobUploads:
    """One serial source session, with explicit optional bounded recovery.

    Composition supplies separate API/store transport authority and owns retirement.
    Successful calls return immutable bytes or a closed private-file receipt.
    Partial or unverified transfers never become consumer results.
    Metadata responses have their API route ceilings; the byte budget counts raw
    store response bodies, including failures. The request/deadline limits cover
    membership, issuance and store calls together, starting at construction.
    Recovery requires an explicit UploadRecovery; its attempt cap cannot refund
    work or extend these budgets. Composition adopts each verified result before
    reporting the current recovery_failure through its protected diagnostic sink.
    """

    def __init__(self, job_ref, *, api, store, store_origin, limits, recovery=None):
        require_job_ref(job_ref)
        if type(limits) is not UploadLimits:
            raise TypeError("Cannot configure Upload acquisition: explicit UploadLimits are required")
        if recovery is not None and type(recovery) is not UploadRecovery:
            raise TypeError("Cannot configure Upload acquisition: recovery must be an explicit UploadRecovery policy")
        self.job_ref = job_ref
        self._api = api
        self._store = store
        self._origin = require_store_origin(store_origin)
        self._limits = limits
        self._recovery = recovery
        self._recovery_failure = None
        self._uncertain_issuance = None
        self._deadline = time.monotonic() + limits.seconds
        self._requests = 0
        self._received = 0
        self._snapshot = None
        self._active = False
        self._grant_issued = False
        self._transfer_owner = UploadTransfer(
            job_ref, store=store, store_origin=store_origin, remaining=self._remaining_seconds,
            before_request=self._reserve_transfer_request, account=self._account_bytes, exchange_owner=self,
        )

    @property
    def last_failure(self):
        return self._transfer_owner.last_failure

    @last_failure.setter
    def last_failure(self, value):
        self._transfer_owner.last_failure = value

    @property
    def recovery_failure(self):
        """Current operation's first outage and first uncertain issuance, if any.

        These are exact original exceptions, retained across result handoff or
        cancellation until the next operation. This is no permission to retry.
        """
        return self._recovery_failure

    def _remember_recovery_failure(self, failure):
        if self._recovery_failure is None:
            self._recovery_failure = failure
        if (type(failure) is ApiFailure and failure.issuance_may_have_committed
                and self._uncertain_issuance is None):
            self._uncertain_issuance = failure
            if self._recovery_failure is not failure:
                self._recovery_failure = BaseExceptionGroup(
                    "Source acquisition recovery observations", (self._recovery_failure, failure)
                )

    def _retain_grant_issuance(self, failure):
        if self._grant_issued and isinstance(failure, UploadFailed) and not failure.grant_issued:
            failure.grant_issued = True
            failure.add_note("Read access was issued before this failure and may have extended Upload retention.")

    def can_retry_transfer(self, failure):
        """Classify the current transfer without dispatching or changing budgets."""
        return not self._active and self._transfer_owner.can_retry_transfer(failure)

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
        if self._transfer_owner._file is not None:
            raise UploadFailed("a private source file remains owned; settle its original I/O and cleanup before more source work")
        if not self._store.retirement_confirmed:
            raise UploadFailed("previous store resource retirement is unconfirmed; retire its transport before more work")
        self._active = True
        self._grant_issued = False
        self._recovery_failure = None
        self._uncertain_issuance = None
        self._transfer_owner._reset_failure()
        try:
            yield
        except BaseException as failure:
            self._remember_recovery_failure(failure)
            self._snapshot = None
            if (isinstance(failure, BaseExceptionGroup) and isinstance(self.last_failure, UploadFailed)
                    and any(cause is self.last_failure for cause in failure.exceptions)):
                self._retain_grant_issuance(self.last_failure)
            elif self._grant_issued:
                # Successful issuance remains true even if later local checks,
                # transfer, or cancellation prevent a usable byte handoff.
                if isinstance(failure, UploadFailed):
                    evidence = failure
                elif isinstance(failure, asyncio.CancelledError) and isinstance(self.last_failure, UploadFailed):
                    evidence = self.last_failure
                else:
                    cause = self.last_failure if isinstance(failure, asyncio.CancelledError) else failure
                    evidence = UploadFailed("transfer did not complete after read access was issued", cause=cause)
                self._retain_grant_issuance(evidence)
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

    def _reserve_transfer_request(self):
        self._require_request_capacity()
        self._requests += 1

    async def _call(self, resource_owner, operation, *args, **kwargs):
        remaining = self._require_request_capacity()
        self._requests += 1
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await operation(*args, **kwargs)
        except ApiFailure as failure:
            self._remember_recovery_failure(failure)
            raise
        except asyncio.CancelledError:
            self.last_failure = resource_owner.last_failure
            if type(resource_owner.last_failure) is ApiFailure:
                self._remember_recovery_failure(resource_owner.last_failure)
            raise
        except TimeoutError:
            if not deadline.expired():
                raise
            if type(resource_owner.last_failure) is ApiFailure:
                self._remember_recovery_failure(resource_owner.last_failure)
            raise UploadFailed("the source session's total deadline expired", cause=resource_owner.last_failure) from None

    def _recovery_exhausted(self, reason, failure):
        if isinstance(failure, UploadFailed):
            exhausted = UploadRecoveryExhausted(
                reason, response=failure.response, cause=failure, cleanup_failures=failure.cleanup_failures,
                interruption=failure.interruption,
            )
            exhausted.grant_issued = failure.grant_issued
        else:
            exhausted = UploadRecoveryExhausted(reason, cause=failure)
        self._retain_grant_issuance(exhausted)
        return exhausted

    def _require_recovery_capacity(self, failure, selected):
        try:
            self._require_request_capacity()
            minimum_requests = 1
            if selected is not None:
                minimum_requests = 3
                self._require_remaining_bytes(selected)
            available = self._limits.requests - self._requests
            if available < minimum_requests:
                raise UploadFailed(
                    f"the next source attempt requires {minimum_requests} requests but only {available} remain"
                )
        except UploadFailed as budget:
            raise self._recovery_exhausted("source recovery exhausted its original budgets: " + budget.reason, failure) from None

    def _require_recovery_wait(self, seconds, failure):
        try:
            remaining = self._require_request_capacity()
        except UploadFailed as exhausted:
            raise self._recovery_exhausted(
                "source recovery exhausted its original budgets: " + exhausted.reason, failure,
            ) from None
        if seconds >= remaining:
            raise self._recovery_exhausted(
                f"source recovery wait of {seconds:g} seconds cannot fit with only {remaining:g} seconds "
                "remaining before the original deadline", failure,
            ) from None

    async def _retire_recovery(self, failure, *, store_owner):
        failures = []
        for operation in (self._api.retire, lambda: self._store.retire(owner=store_owner)):
            try:
                await contain_task_exit(operation)
            except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cleanup:
                failures.append(cleanup)
        if self._store.retirement_confirmed and self._transfer_owner.file_cleanup_pending:
            try:
                await contain_task_exit(self._transfer_owner.settle_file)
            except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cleanup:
                failures.append(cleanup)
        if not failures and (
            not self._api.retirement_confirmed or not self._store.retirement_confirmed
            or self._transfer_owner.file_cleanup_pending
        ):
            failures.append(UploadFailed("original source exchange or private file retirement remains unconfirmed"))
        if not failures:
            return
        cancellation = next((cause for cause in failures if isinstance(cause, asyncio.CancelledError)), None)
        previous_cause = None
        if cancellation is not None:
            previous_cause = cancellation.__cause__
        record = UploadFailed(
            "source recovery could not retire the original exchange or private file",
            cause=failure,
            cleanup_failures=[cause for cause in failures if cause is not cancellation],
            interruption=previous_cause,
        )
        self.last_failure = record
        if cancellation is not None:
            raise cancellation from record
        fatal = [cause for cause in failures if not isinstance(cause, Exception)]
        if fatal:
            raise BaseExceptionGroup("Source recovery retirement raised process-control exceptions", (*fatal, record)) from None
        raise record from None

    async def _recover_source(self, operation, *, selected=None, store_owner=None):
        policy = self._recovery
        if policy is None:
            return await operation()
        delay = policy.initial_seconds
        retired_outage = None
        for attempt in range(policy.maximum_attempts):
            if policy.stopping.is_set():
                raise UploadFailed("source acquisition stopped before another request", cause=self.recovery_failure)
            if attempt:
                self._require_recovery_capacity(retired_outage, selected)
                self._transfer_owner._reset_failure()
                retired_outage = None
            try:
                return await operation()
            except Exception as failure:
                self._retain_grant_issuance(failure)
                self._remember_recovery_failure(failure)
                self.last_failure = failure
                await self._retire_recovery(failure, store_owner=store_owner)
                api_failure = failure
                if isinstance(failure, UploadFailed) and type(failure.cause) is ApiFailure:
                    api_failure = failure.cause
                api_permitted = (
                    type(api_failure) is ApiFailure and (
                        self._api.can_retry_read(api_failure)
                        or selected is not None and self._api.can_renew_read_grant(
                            api_failure, job_ref=self.job_ref, upload_ref=selected.upload_ref,
                        )
                    )
                )
                if not (api_permitted or self._transfer_owner.can_retry_transfer(failure)):
                    raise
                if policy.stopping.is_set():
                    raise UploadFailed("source acquisition stopped before another request", cause=self.recovery_failure)
                retired_outage = failure
                if attempt + 1 == policy.maximum_attempts:
                    raise self._recovery_exhausted("source recovery exhausted its configured attempt limit", failure) from None
                self._require_recovery_capacity(failure, selected)
                floor = None
                if api_permitted:
                    floor = api_failure.retry_after_seconds
                local_wait = secrets.SystemRandom().uniform(delay / 2, delay)
                seconds = local_wait if floor is None else max(local_wait, floor)
                self._require_recovery_wait(seconds, failure)
                if policy.stopping.is_set():
                    raise UploadFailed("source acquisition stopped before another request", cause=self.recovery_failure)
                try:
                    policy.on_retry(failure, seconds)
                except Exception as notice:
                    raise UploadFailed(
                        "source recovery notification failed before another request",
                        cause=BaseExceptionGroup("Source outage and notification failure", (self.recovery_failure, notice)),
                    ) from None
                if policy.stopping.is_set():
                    raise UploadFailed("source acquisition stopped before another request", cause=self.recovery_failure)
                self._require_recovery_wait(seconds, failure)
                try:
                    await asyncio.wait_for(policy.stopping.wait(), timeout=seconds)
                except TimeoutError:
                    delay = min(policy.maximum_seconds, delay * 2)
                    continue
                raise UploadFailed("source acquisition stopped before another request", cause=self.recovery_failure)

    async def inspect(self):
        """Refresh the source list; callers choose from this observation."""
        async with self._operation():
            async def observe():
                self._snapshot = await self._call(self._api, self._api.uploads, self.job_ref)
                return self._snapshot
            return await self._recover_source(observe)

    async def acquire(self, upload_ref):
        """Verify a selected source, optionally recovering within original budgets.

        Every fresh grant requires the original inspected membership unchanged.
        """
        async with self._operation():
            selected = self._select(upload_ref)
            async def acquire():
                grant = await self._grant(selected)
                return await self._transfer_owner._download(grant)
            return await self._recover_source(acquire, selected=selected)

    def _select(self, upload_ref):
        require_upload_ref(upload_ref)
        self._require_request_capacity()
        if self._snapshot is None:
            raise UploadInputRejected("inspect the current Job Uploads before selecting a source")
        selected = next((item for item in self._snapshot.members if item.upload_ref == upload_ref), None)
        if selected is None:
            raise UploadInputRejected("selected Upload is not in the inspected Job membership; inspect and select again")
        # Correction cannot revive spent work. Neither refusal issues a grant.
        self._require_remaining_bytes(selected)
        if selected.byte_length > self._limits.per_source_bytes:
            raise UploadInputRejected(
                f"selected Upload has {selected.byte_length} bytes and exceeds the fixed "
                f"{self._limits.per_source_bytes}-byte per-source limit; select a smaller Upload"
            )
        return selected

    def _require_remaining_bytes(self, selected):
        if selected.byte_length > self._limits.total_bytes - self._received:
            raise UploadFailed("selected Upload exceeds the source session's remaining byte budget")

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
        """Verify a closed file, optionally recovering the same selected Upload."""
        async with self._operation():
            selected = self._select(upload_ref)
            async def acquire():
                return await self._transfer_owner.acquire_file(
                    lambda: self._grant(selected), directory_fd=directory_fd, retirement_seconds=retirement_seconds,
                )
            return await self._recover_source(acquire, selected=selected, store_owner=self)

    async def settle_file(self):
        """Observe the original private-file cleanup without starting a new transfer."""
        if self._active:
            raise UploadFailed("another source operation is active")
        return await self._transfer_owner.settle_file()

    def _account_bytes(self, count):
        # Count the offending chunk too: bytes already consumed cannot be refunded.
        self._received += count
        if self._received > self._limits.total_bytes:
            raise UploadFailed(f"download exhausted the source session's {self._limits.total_bytes}-byte budget")



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
