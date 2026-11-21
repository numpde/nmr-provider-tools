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
import os
import reprlib
import secrets
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

import httpcore
import httpx

from provider_core.api_errors import ApiFailure
from provider_core.adapters.nmr.v1.upload_store import download_request, download_target, verify_store_headers
from provider_core.task_boundary import contain_task_exit
from provider_core.transport import ExchangeFailed, temporary_io_failure
from provider_core.response_json import ResponseRejected
from provider_core.tools import require_positive_number
from provider_core.validation.upstream.http import has_transfer_encoding
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.validation.upstream.nmr.v1.upload_response import require_read_grant, require_upload_ref


from provider_core.upload_file import (
    UploadFailed,
    UploadFileReadFailed,
    UploadInputRejected,
    VerifiedUpload,
    VerifiedUploadFile,
    read_upload_file,
)

from provider_core.type_requirements import require_type, require_instance


# Keep admitted grant URLs complete while bounding rejected configuration text
# repo-check: allow-module-constant
_UPLOAD_EVIDENCE = reprlib.Repr()

_UPLOAD_EVIDENCE.maxstring = 512


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """Explicit policy for one source session; repeated calls do not reset it."""

    per_source_bytes: int
    total_bytes: int
    requests: int
    seconds: float

    def __post_init__(self):
        require_type(self.per_source_bytes, int)

        if not (0 < self.per_source_bytes):
            raise ValueError(f"{self.per_source_bytes=} should be positive")

        require_type(self.total_bytes, int)

        if not (0 < self.total_bytes):
            raise ValueError(f"{self.total_bytes=} should be positive")

        require_type(self.requests, int)

        if not (0 < self.requests):
            raise ValueError(f"{self.requests=} should be positive")

        require_positive_number(self.seconds)


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
        require_type(self.maximum_attempts, int)

        if not (0 < self.maximum_attempts):
            raise ValueError(f"{self.maximum_attempts=} should be positive")

        for seconds in (self.initial_seconds, self.maximum_seconds):
            require_positive_number(seconds)

        if not (self.initial_seconds <= self.maximum_seconds):
            raise ValueError(f"{self.initial_seconds=} should not exceed {self.maximum_seconds=}")

        require_instance(self.stopping, asyncio.Event)

        if not callable(self.on_retry):
            raise TypeError(f"{self.on_retry=} should be callable")


def require_store_origin(store_origin):
    """Require an explicit canonical origin before any bearer can be attached."""

    require_type(store_origin, str)

    # Reject characters that URL parsing could silently normalize
    if not all((32 < (origin_codepoint := ord(character)) < 127) for character in store_origin):
        raise ValueError(f"{origin_codepoint=} should be visible ASCII")

    try:
        store_url = httpx.URL(store_origin)
    except httpx.InvalidURL as cause:
        raise ValueError(str(cause)) from cause

    expected_scheme = "https"

    if not (store_url.scheme == expected_scheme):
        raise ValueError(f"{store_url.scheme=} should match {expected_scheme=}")

    if not (store_url.host):
        raise ValueError(f"{store_url.host=} should be nonempty")

    if not (store_url.userinfo == b""):
        raise ValueError(f"{bool(store_url.userinfo)=} should be False")

    if not (store_url.query == b""):
        raise ValueError(f"{len(store_url.query)=} should be zero")

    if not (store_url.fragment == ""):
        raise ValueError(f"{len(store_url.fragment)=} should be zero")

    expected_path = b"/"

    if not (store_url.raw_path == expected_path):
        raise ValueError(f"{_UPLOAD_EVIDENCE.repr(store_url.raw_path)=} should match {expected_path=}")

    canonical_store_origin = str(store_url)

    if not (canonical_store_origin == store_origin):
        raise ValueError(f"{_UPLOAD_EVIDENCE.repr(store_origin)=} should match {_UPLOAD_EVIDENCE.repr(canonical_store_origin)=}")

    if not (not store_origin.endswith("/")):
        raise ValueError(f"{_UPLOAD_EVIDENCE.repr(store_origin)=} should not end with '/'")

    return store_origin


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
        directory_stat = os.fstat(self.directory_fd)

        if not stat.S_ISDIR(directory_stat.st_mode):
            raise UploadFailed(f"{self.directory_fd=} has bad {directory_stat.st_mode=}")

        expected_directory_uid = os.geteuid()

        if not (directory_stat.st_uid == expected_directory_uid):
            raise UploadFailed(f"{directory_stat.st_uid=} should match {expected_directory_uid=} for {self.directory_fd=}")

        directory_permissions = stat.S_IMODE(directory_stat.st_mode)
        expected_directory_permissions = 0o700

        if not (directory_permissions == expected_directory_permissions):
            raise UploadFailed(f"{directory_permissions=:o} should match {expected_directory_permissions=:o} for {self.directory_fd=}")

        self.fd = os.open(
            self.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )

        self.created = True

        file_stat = os.fstat(self.fd)
        self.identity = (file_stat.st_dev, file_stat.st_ino)

        os.fchmod(self.fd, 0o600)

    def _close(self):
        (descriptor, self.fd) = (self.fd, None)

        try:
            os.close(descriptor)
        except OSError:
            # The kernel may have retired and reused the number despite failure.
            self.close_uncertain = True

            raise

    def _remove(self):
        try:
            entry_stat = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            self.created = False

            return

        if self.identity is None:
            raise UploadFailed(f"{self.identity=} should be recorded before removing {self.name=} in {self.directory_fd=}")

        entry_identity = (entry_stat.st_dev, entry_stat.st_ino)

        if not (entry_identity == self.identity):
            raise UploadFailed(f"{entry_identity=} should match {self.identity=} for {self.name=} in {self.directory_fd=}")

        os.unlink(self.name, dir_fd=self.directory_fd)
        self.created = False

    async def create(self, seconds):
        await self._start_file_io("create", self._create, seconds)

    async def close(self, seconds):
        await self._start_file_io("close", self._close, seconds)

    async def remove(self, seconds):
        await self._start_file_io("remove", self._remove, seconds)

    async def _start_file_io(self, operation, function, seconds):
        worker = self._executor.submit(function)
        waiter = asyncio.wrap_future(worker)

        waiter.add_done_callback(_observe_file_io)
        self.pending = _FileIO(operation, worker, waiter)

        await self.settle(seconds)

    async def settle(self, seconds):
        pending = self.pending
        (done, _unfinished) = await asyncio.wait({pending.waiter}, timeout=seconds)

        if not (done):
            raise UploadFailed(
                f"{pending.operation=} for {self.name=} in {self.directory_fd=} was not observed",
                cause=TimeoutError(f"File observation exceeded {seconds=}"),
            )

        self.pending = None

        try:
            pending.worker.result()
        except Exception as cause:
            raise UploadFailed(
                f"Cannot complete {pending.operation=} for {self.name=} in {self.directory_fd=}",
                cause=cause
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

        if not callable(remaining):
            raise TypeError(f"{remaining=} should be callable")

        if not callable(before_request):
            raise TypeError(f"{before_request=} should be callable")

        if not callable(account):
            raise TypeError(f"{account=} should be callable")

        (self.job_ref, self._store) = (job_ref, store)
        self._origin = require_store_origin(store_origin)

        (self._remaining, self._before_request, self._account_bytes) = (remaining, before_request, account)
        self._exchange_owner = self if exchange_owner is None else exchange_owner
        self._pending_upload_file = None
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
            (failure is not self.last_failure) or
            (not isinstance(failure, UploadFailed)) or
            (failure.interruption is not None) or
            self._interrupted or
            self._active or
            (not self._store.retirement_confirmed) or
            self.file_cleanup_pending or
            (self._transfer_grant is None)
        ):
            return False

        outcome = self._store_outcome

        if isinstance(outcome, ExchangeFailed):
            if (
                (failure.cause is not outcome) or
                (outcome is not self._store.last_failure) or
                outcome.cleanup_failures or
                (outcome.interruption is not None)
            ):
                return False

            head = outcome.response_head

            if (self._header_failure is not None) and (outcome.primary is self._header_failure):
                return (
                    (head is not None) and
                    (self._header_failure.response is head) and
                    (head.status in {401, 503, 507})
                )

            if outcome.primary is not outcome.network_failure:
                return False

            if head is not None:
                try:
                    verify_store_headers(head, self._transfer_grant)
                # silent-defaults: allow-exception-outcome -- Unadmitted whole-file headers grant no retry; the original transfer failure remains retained.
                except UploadFailed:
                    return False

            if (head is not None) and (has_transfer_encoding(head.headers)):
                return False

            if type(outcome.primary) is httpcore.RemoteProtocolError:
                return head is not None

            return temporary_io_failure(outcome.primary, 8, network_failure=outcome.network_failure)

        return False

    @property
    def file_cleanup_pending(self):
        """Report whether this owner still retains an unresolved private file."""

        return self._pending_upload_file is not None

    def _remaining_seconds(self):
        remaining_seconds = self._remaining()

        try:
            return require_positive_number(remaining_seconds)
        except (TypeError, ValueError) as cause:
            raise UploadFailed(f"{_UPLOAD_EVIDENCE.repr(remaining_seconds)=} should be finite and positive") from cause

    @asynccontextmanager
    async def _operation(self):
        if self._active:
            raise UploadFailed(f"{self._active=} should be False before another transfer")

        if not (self._pending_upload_file is None):
            raise UploadFailed(f"{self._pending_upload_file is None=} should be True before another transfer")

        if not (self._store.retirement_confirmed):
            raise UploadFailed(f"{self._store.retirement_confirmed=} should be True before another transfer")

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
                raise TypeError(f"{grant_factory=} should be callable")

            require_type(directory_fd, int, error_factory=UploadFailed)

            if not (0 <= directory_fd):
                raise UploadFailed(f"{directory_fd=} should be nonnegative")

            try:
                require_positive_number(retirement_seconds)
            except (TypeError, ValueError) as cause:
                raise UploadFailed(f"{_UPLOAD_EVIDENCE.repr(retirement_seconds)=} should be finite and positive") from cause

            pending_upload_file = self._pending_upload_file = _UploadFile(directory_fd, retirement_seconds)

            try:
                await pending_upload_file.create(self._remaining_seconds())
                grant_wait_seconds = self._remaining_seconds()
                deadline = asyncio.timeout(grant_wait_seconds)

                try:
                    async with deadline:
                        grant = await grant_factory()
                except TimeoutError as failure:
                    if not deadline.expired():
                        raise

                    prior = self.last_failure

                    if prior is None:
                        raise UploadFailed(
                            f"The read grant did not arrive within {grant_wait_seconds=}",
                            cause=failure,
                        ) from None

                    raise UploadFailed(
                        f"The read grant did not arrive within {grant_wait_seconds=}",
                        cause=prior,
                        interruption=failure,
                    ) from None

                downloaded_file = await self._transfer(grant, output_fd=pending_upload_file.fd)

                if not (downloaded_file.byte_length == grant.byte_length):
                    raise UploadFailed(f"{downloaded_file.byte_length=} should match {grant.byte_length=}")

                if not (downloaded_file.content_hash == grant.content_hash):
                    raise UploadFailed(f"{downloaded_file.content_hash=} should match {grant.content_hash=}")

                await pending_upload_file.close(self._remaining_seconds())
                self._remaining_seconds()
                verified_upload_file = VerifiedUploadFile(self.job_ref, grant.upload_ref, grant.byte_length, grant.content_hash, pending_upload_file.name)

                pending_upload_file.shutdown()
                self._pending_upload_file = None

                return verified_upload_file
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
                self.last_failure if (isinstance(failure, asyncio.CancelledError)) and (self.last_failure is not None) else failure
            )

            self.last_failure = UploadFailed("private-file acquisition did not complete", cause=cause)

    def _remember_file_interruption(self, cause):
        retained = self.last_failure.interruption

        if retained is None:
            self.last_failure.interruption = cause
        elif (cause is not None) and (cause is not retained):
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
                "input selection was rejected and private-file cleanup remains unresolved",
                cause=self.last_failure
            )

        # A pending wait supplies no new filesystem result. Keep the concrete
        # cleanup cause already retained, without accumulating polling history.
        if (not self.last_failure.cleanup_failures) or (self._pending_upload_file.pending is None):
            self.last_failure.cleanup_failures = (failure,)

        if self._pending_upload_file.close_uncertain and (self._pending_upload_file.pending is None):
            self._pending_upload_file.shutdown()

    async def _abort_file(self):
        pending_upload_file = self._pending_upload_file
        deadline = time.monotonic() + pending_upload_file.retirement_seconds

        def remaining():
            cleanup_seconds = deadline - time.monotonic()

            if cleanup_seconds <= 0:
                raise UploadFailed(f"{cleanup_seconds=} should be positive")

            return cleanup_seconds

        if pending_upload_file.pending is not None:
            operation = pending_upload_file.pending.operation

            try:
                await pending_upload_file.settle(remaining())
            except UploadFailed as failure:
                if (pending_upload_file.pending is not None) or (operation != "create"):
                    raise

                # No grant can precede an observed create. Its late intrinsic
                # failure replaces only that create wait's interruption cause.
                self._remember_file_interruption(self.last_failure.cause)

                self.last_failure.cause = failure

        if pending_upload_file.close_uncertain:
            raise UploadFailed(f"{pending_upload_file.close_uncertain=} requires process retirement")

        if pending_upload_file.fd is not None:
            await pending_upload_file.close(remaining())

        if pending_upload_file.created:
            await pending_upload_file.remove(remaining())

        pending_upload_file.shutdown()
        self._pending_upload_file = None

    async def settle_file(self):
        """Confirm abort cleanup only, without resuming a grant or download.

        Composition must first retire the exact store exchange with the
        configured exchange owner. The retained cleanup grace works after the
        source deadline expires; acquisition budgets never reset.
        """

        if self._active:
            raise UploadFailed(f"{self._active=} should be False before file settlement")

        if self._pending_upload_file is None:
            raise UploadFailed(f"{self._pending_upload_file is not None=} should be True before file settlement")

        if not (self._store.retirement_confirmed):
            raise UploadFailed(
                f"{self._store.retirement_confirmed=} should be True before file settlement",
                cause=self.last_failure,
            )

        if self._pending_upload_file.close_uncertain and (self._pending_upload_file.pending is None):
            raise UploadFailed(
                f"{self._pending_upload_file.close_uncertain=} requires process retirement",
                cause=self.last_failure
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
                f"{self._pending_upload_file.name=} in {self._pending_upload_file.directory_fd=} still needs cleanup",
                cause=self.last_failure
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
            raise UploadFailed(str(failure), cause=failure) from None

        expected_download_url = download_target(self._origin, grant.upload_ref)

        if not (grant.download_url == expected_download_url):
            raise UploadFailed(f"{_UPLOAD_EVIDENCE.repr(grant.download_url)=} should match {_UPLOAD_EVIDENCE.repr(expected_download_url)=}")

        grant_checked_at = datetime.now(UTC)

        if not (grant.expires_at > grant_checked_at):
            raise UploadFailed(f"{grant.expires_at=} should be after {grant_checked_at=}")

        return download_request(expected_download_url, capability=grant.capability)

    async def _transfer(self, grant, *, output_fd=None):
        download_http_request = self._download_request(grant)

        self._before_request()
        transfer_seconds = self._remaining_seconds()
        deadline = asyncio.timeout(transfer_seconds)
        self._transfer_grant = grant

        def admit(head):
            try:
                verify_store_headers(head, grant)
            except UploadFailed as failure:
                self._header_failure = failure

                raise

        try:
            async with deadline:
                if output_fd is None:
                    response = await self._store.exchange(
                        download_http_request,
                        maximum_bytes=grant.byte_length,
                        account_bytes=self._account_bytes,
                        before_body=admit,
                    )
                else:
                    response = await self._store.exchange_file(
                        download_http_request,
                        output_fd=output_fd,
                        maximum_bytes=grant.byte_length,
                        account_bytes=self._account_bytes,
                        owner=self._exchange_owner,
                        before_body=admit,
                    )

                self._store_outcome = response

                return response
        except TimeoutError:
            if not deadline.expired():
                raise

            raise UploadFailed(f"The store exchange exceeded {transfer_seconds=}", cause=self._store.last_failure) from None
        except asyncio.CancelledError:
            self._interrupted = True
            self._store_outcome = self._store.last_failure

            if (self.last_failure is None) and (self._store.last_failure is not None):
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

        require_type(limits, UploadLimits)

        if recovery is not None:
            require_type(recovery, UploadRecovery)

        self.job_ref = job_ref
        self._api = api
        self._store = store
        self._origin = require_store_origin(store_origin)
        self._limits = limits
        self._recovery = recovery
        self._recovery_failure = None
        self._uncertain_issuance = None
        self._deadline = time.monotonic() + limits.seconds
        self._requests_started = 0
        self._received_bytes = 0
        self._inspected_membership = None
        self._active = False
        self._grant_issued = False

        self._transfer_owner = UploadTransfer(
            job_ref,
            store=store,
            store_origin=store_origin,
            remaining=self._remaining_seconds,
            before_request=self._reserve_transfer_request,
            account=self._account_bytes,
            exchange_owner=self,
        )

    @property
    def last_failure(self):
        return self._transfer_owner.last_failure

    @last_failure.setter
    def last_failure(self, failure):
        self._transfer_owner.last_failure = failure

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

        if (
            (type(failure) is ApiFailure) and
            failure.issuance_may_have_committed and
            (self._uncertain_issuance is None)
        ):
            self._uncertain_issuance = failure

            if self._recovery_failure is not failure:
                self._recovery_failure = BaseExceptionGroup(
                    "Source acquisition recovery observations",
                    (self._recovery_failure, failure)
                )

    def _retain_grant_issuance(self, failure):
        if (self._grant_issued) and (isinstance(failure, UploadFailed)) and (not failure.grant_issued):
            failure.grant_issued = True

            failure.add_note("Read access was issued before this failure and may have extended Upload retention.")

    def can_retry_transfer(self, failure):
        """Classify the current transfer without dispatching or changing budgets."""

        return (not self._active) and (self._transfer_owner.can_retry_transfer(failure))

    @property
    def received_bytes(self):
        return self._received_bytes

    @property
    def requests_started(self):
        return self._requests_started

    @asynccontextmanager
    async def _operation(self):
        if self._active:
            raise UploadFailed(f"{self._active=} should be False before another acquisition operation")

        if not (self._transfer_owner._pending_upload_file is None):
            raise UploadFailed(f"{self._transfer_owner.file_cleanup_pending=} should be False before another acquisition operation")

        if not (self._store.retirement_confirmed):
            raise UploadFailed(f"{self._store.retirement_confirmed=} should be True before another acquisition operation")

        self._active = True
        self._grant_issued = False
        self._recovery_failure = None
        self._uncertain_issuance = None

        self._transfer_owner._reset_failure()

        try:
            yield
        except BaseException as failure:
            self._remember_recovery_failure(failure)
            self._inspected_membership = None

            if (
                isinstance(failure, BaseExceptionGroup) and
                isinstance(self.last_failure, UploadFailed) and
                any((cause is self.last_failure) for cause in failure.exceptions)
            ):
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
        remaining_seconds = self._deadline - time.monotonic()

        if remaining_seconds <= 0:
            raise UploadFailed(f"{remaining_seconds=} should be positive")

        return remaining_seconds

    def _require_request_capacity(self):
        remaining_seconds = self._remaining_seconds()

        if not (self._requests_started < self._limits.requests):
            raise UploadFailed(f"{self._requests_started=} should be below {self._limits.requests=}")

        return remaining_seconds

    def _reserve_transfer_request(self):
        self._require_request_capacity()
        self._requests_started += 1

    async def _call(self, resource_owner, operation, *args, **kwargs):
        remaining_seconds = self._require_request_capacity()
        self._requests_started += 1
        deadline = asyncio.timeout(remaining_seconds)

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

            raise UploadFailed(f"The API reply did not arrive within {remaining_seconds=}", cause=resource_owner.last_failure) from None

    def _recovery_exhausted(self, reason, failure):
        if isinstance(failure, UploadFailed):
            exhausted = UploadRecoveryExhausted(
                reason,
                response=failure.response,
                cause=failure,
                cleanup_failures=failure.cleanup_failures,
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

            available_requests = self._limits.requests - self._requests_started

            if not (minimum_requests <= available_requests):
                raise UploadFailed(f"{available_requests=} should be at least {minimum_requests=}")
        except UploadFailed as budget:
            raise self._recovery_exhausted(budget.reason, failure) from None

    def _require_recovery_wait(self, recovery_wait_seconds, failure):
        try:
            remaining_seconds = self._require_request_capacity()
        except UploadFailed as exhausted:
            raise self._recovery_exhausted(exhausted.reason, failure) from None

        if not (recovery_wait_seconds < remaining_seconds):
            raise self._recovery_exhausted(
                f"{recovery_wait_seconds=} should be below {remaining_seconds=}",
                failure,
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

        if not failures:
            if not self._api.retirement_confirmed:
                failures.append(UploadFailed(f"{self._api.retirement_confirmed=} should be True before recovery"))
            elif not self._store.retirement_confirmed:
                failures.append(UploadFailed(f"{self._store.retirement_confirmed=} should be True before recovery"))
            elif self._transfer_owner.file_cleanup_pending:
                failures.append(UploadFailed(f"{self._transfer_owner.file_cleanup_pending=} should be False before recovery"))

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

        if not (cancellation is None):
            raise cancellation from record

        fatal = [cause for cause in failures if not isinstance(cause, Exception)]

        # repo-check: allow-literal-guard-error -- The original process-control failures and retained source record provide the group's evidence
        if fatal:
            raise BaseExceptionGroup("Source recovery retirement raised process-control exceptions", (*fatal, record)) from None

        raise record from None

    async def _recover_source(self, operation, *, selected=None, store_owner=None):
        recovery_policy = self._recovery

        if recovery_policy is None:
            return await operation()

        delay = recovery_policy.initial_seconds
        retired_outage = None

        for attempt_index in range(recovery_policy.maximum_attempts):
            if recovery_policy.stopping.is_set():
                raise UploadFailed(f"{recovery_policy.stopping.is_set()=} should be False before another request", cause=self.recovery_failure)

            if attempt_index:
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

                if isinstance(failure, UploadFailed) and (type(failure.cause) is ApiFailure):
                    api_failure = failure.cause

                api_permitted = (
                    (type(api_failure) is ApiFailure) and
                    (self._api.can_retry_read(api_failure) or
                     ((selected is not None) and
                      self._api.can_renew_read_grant(
                          api_failure,
                          job_ref=self.job_ref,
                          upload_ref=selected.upload_ref,
                      )))
                )

                if not (api_permitted or self._transfer_owner.can_retry_transfer(failure)):
                    raise

                if recovery_policy.stopping.is_set():
                    raise UploadFailed(f"{recovery_policy.stopping.is_set()=} should be False before another request", cause=self.recovery_failure)

                retired_outage = failure
                attempts_started = attempt_index + 1

                if not (attempts_started < recovery_policy.maximum_attempts):
                    raise self._recovery_exhausted(
                        f"{attempts_started=} should be below {recovery_policy.maximum_attempts=}",
                        failure,
                    ) from None

                self._require_recovery_capacity(failure, selected)
                floor = None

                if api_permitted:
                    floor = api_failure.retry_after_seconds

                local_wait = secrets.SystemRandom().uniform(delay / 2, delay)
                seconds = local_wait if floor is None else max(local_wait, floor)

                self._require_recovery_wait(seconds, failure)

                if recovery_policy.stopping.is_set():
                    raise UploadFailed(f"{recovery_policy.stopping.is_set()=} should be False before another request", cause=self.recovery_failure)

                try:
                    recovery_policy.on_retry(failure, seconds)
                except Exception as notice:
                    raise UploadFailed(
                        f"{type(notice)=} interrupted the recovery notification",
                        cause=BaseExceptionGroup("Source outage and notification failure", (self.recovery_failure, notice)),
                    ) from None

                if recovery_policy.stopping.is_set():
                    raise UploadFailed(f"{recovery_policy.stopping.is_set()=} should be False before another request", cause=self.recovery_failure)

                self._require_recovery_wait(seconds, failure)

                try:
                    await asyncio.wait_for(recovery_policy.stopping.wait(), timeout=seconds)
                except TimeoutError:
                    delay = min(recovery_policy.maximum_seconds, delay * 2)

                    continue

                raise UploadFailed(f"{recovery_policy.stopping.is_set()=} should be False before another request", cause=self.recovery_failure)

    async def inspect(self):
        """Refresh the source list; callers choose from this observation."""

        async with self._operation():
            async def observe():
                self._inspected_membership = await self._call(self._api, self._api.uploads, self.job_ref)

                return self._inspected_membership

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

        if self._inspected_membership is None:
            raise UploadInputRejected(f"{self._inspected_membership=} should be populated by inspect() before source selection")

        selected = next((upload for upload in self._inspected_membership.members if upload.upload_ref == upload_ref), None)

        if selected is None:
            raise UploadInputRejected(f"{upload_ref=} should belong to the inspected membership for {self.job_ref=}")

        # Correction cannot revive spent work. Neither refusal issues a grant.
        self._require_remaining_bytes(selected)

        if not (selected.byte_length <= self._limits.per_source_bytes):
            raise UploadInputRejected(f"{selected.byte_length=} should not exceed {self._limits.per_source_bytes=}")

        return selected

    def _require_remaining_bytes(self, selected):
        remaining_upload_bytes = self._limits.total_bytes - self._received_bytes

        if not (selected.byte_length <= remaining_upload_bytes):
            raise UploadFailed(f"{selected.byte_length=} should not exceed {remaining_upload_bytes=}")

    async def _grant(self, selected):
        refreshed_membership = await self._call(self._api, self._api.uploads, self.job_ref)

        if refreshed_membership != self._inspected_membership:
            self._require_request_capacity()

            if refreshed_membership.job_ref != self._inspected_membership.job_ref:
                raise UploadInputRejected(f"{refreshed_membership.job_ref=} should match {self._inspected_membership.job_ref=}")

            if len(refreshed_membership.members) != len(self._inspected_membership.members):
                raise UploadInputRejected(f"{len(refreshed_membership.members)=} should match {len(self._inspected_membership.members)=}")

            changed_member_fields = {}

            for (member_index, (inspected_member, refreshed_member)) in enumerate(
                zip(self._inspected_membership.members, refreshed_membership.members),
            ):
                if inspected_member == refreshed_member:
                    continue

                changed_member_fields[member_index] = [
                    member_field.name
                    for member_field in fields(refreshed_member)
                    if getattr(refreshed_member, member_field.name) != getattr(inspected_member, member_field.name)
                ]

                break

            raise UploadInputRejected(f"{changed_member_fields=} should be empty before issuing read access")

        grant = await self._call(self._api, self._api.read_grant, self.job_ref, selected.upload_ref)
        self._grant_issued = True

        if not (grant.job_ref == self.job_ref):
            raise UploadFailed(f"{_UPLOAD_EVIDENCE.repr(grant.job_ref)=} should match {self.job_ref=}")

        if not (grant.upload_ref == selected.upload_ref):
            raise UploadFailed(f"{_UPLOAD_EVIDENCE.repr(grant.upload_ref)=} should match {selected.upload_ref=}")

        if not (grant.byte_length == selected.byte_length):
            raise UploadFailed(f"{grant.byte_length=} should match {selected.byte_length=}")

        if (selected.content_hash is not None) and (grant.content_hash != selected.content_hash):
            raise UploadFailed(f"{grant.content_hash=} should match {selected.content_hash=}")

        return grant

    async def acquire_file(self, upload_ref, *, directory_fd, retirement_seconds):
        """Verify a closed file, optionally recovering the same selected Upload."""

        async with self._operation():
            selected = self._select(upload_ref)

            async def acquire():
                return await self._transfer_owner.acquire_file(
                    lambda: self._grant(selected),
                    directory_fd=directory_fd,
                    retirement_seconds=retirement_seconds,
                )

            return await self._recover_source(acquire, selected=selected, store_owner=self)

    async def settle_file(self):
        """Observe the original private-file cleanup without starting a new transfer."""

        if self._active:
            raise UploadFailed(f"{self._active=} should be False before file settlement")

        return await self._transfer_owner.settle_file()

    def _account_bytes(self, chunk_bytes):
        # Count the offending chunk too: bytes already consumed cannot be refunded.
        self._received_bytes += chunk_bytes

        if not (self._received_bytes <= self._limits.total_bytes):
            raise UploadFailed(f"{self._received_bytes=} should not exceed {self._limits.total_bytes=}")
