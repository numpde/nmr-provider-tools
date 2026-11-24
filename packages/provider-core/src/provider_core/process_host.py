"""One installed scientific process per allocation, with retained SDK owners.

The application uses the existing AnalysisExecution/Operation contracts. Worker
loss is never non-admission or permission to run the original science again.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import reprlib
import socket
import time
from threading import Lock
from uuid import uuid4

from provider_core._embedded_support import EMBEDDED_CAPABILITIES, _CleanupState, _require_supported_binding
from provider_core.async_runner_session import AsyncRunnerSession, await_original
from provider_core.contribution import ResourceRequirement
from provider_core.hosting import (
    _require_positive_capacity,
    AcceptanceState,
    AdmissionCode,
    AllocationRef,
    CapacityUnavailable,
    CleanupObservation,
    DispatchState,
    Failure,
    Invocation,
    NotAdmitted,
    Observation,
    OperationRef,
    RequestConflict,
    ReservationRef,
    ResourceState,
    ResultRef,
    ResultState,
    StartDeadlineExpired,
    _raise_context_failure,
    _start_owned_task,
    settle_cleanup,
)
from provider_core.process_wire import MAX_PAYLOAD_BYTES, RemoteScienceError, decoded_error_evidence, frame, receive
from provider_core.runner_session import OriginalWorkerExit, RunnerSession, RunnerStartupStopped, RunnerStartupUnavailable
from provider_core.scientific import Contribution, HostedContribution
from provider_core.tools import require_nonnegative_number, require_positive_number, require_running_loop
from provider_core.type_requirements import require_instance, require_type


class _Channel:
    """One bounded request/reply at a time; a lost reply retires the channel."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = Lock()
        self.failure = None

    def fail(self, cause):
        if self.failure is None:
            self.failure = cause

        return RemoteScienceError("Scientific process reply is unknown; retain its original work")

    def exchange(self, message, seconds=2, *, accept=None):
        # Framing is a local refusal. Once bytes may have been sent, all
        # failures belong to the original retained request.
        request_frame = frame(message)

        return self.exchange_frame(request_frame, deadline=time.monotonic() + seconds, accept=accept)

    def exchange_frame(self, request_frame, *, deadline, accept=None):
        remaining = max(0, deadline - time.monotonic())

        if not self.lock.acquire(timeout=remaining):
            raise TimeoutError(f"Scientific channel remained occupied through {deadline=}; no request bytes were sent")

        try:
            if not (self.failure is None):
                raise RemoteScienceError(f"Cannot reuse the scientific channel after {type(self.failure)=}") from self.failure

            remaining = deadline - time.monotonic()

            if not (0 < remaining):
                raise TimeoutError(f"{deadline=} expired with {remaining=}; no request bytes were sent")

            try:
                self.sock.settimeout(remaining)
                self.sock.sendall(request_frame)
                response_envelope = receive(self.sock, deadline=deadline)

                require_type(response_envelope, dict, error_factory=ValueError)
                require_type(response_envelope.get("ok"), bool, error_factory=ValueError)

                if response_envelope["ok"]:
                    if not (set(response_envelope) == (expected_response_fields := {"ok", "value"})):
                        raise ValueError(f"{reprlib.repr(set(response_envelope))=} ≠ {expected_response_fields=}")

                    return response_envelope["value"] if accept is None else accept(response_envelope["value"])

                if not (set(response_envelope) == (expected_response_fields := {"ok", "error"})):
                    raise ValueError(f"{reprlib.repr(set(response_envelope))=} ≠ {expected_response_fields=}")

                require_instance(response_envelope["error"], BaseException, error_factory=ValueError)
            except BaseException as cause:
                failure = self.fail(cause)

                if not isinstance(cause, Exception):
                    raise

                raise failure from cause

            # A well-formed remote failure is evidence, not a corrupt frame.
            # Mutating callers decide whether their request is now uncertain.
            if not (response_envelope["ok"]):
                raise response_envelope["error"]
        finally:
            self.lock.release()

    def close(self):
        self.sock.close()

    def interrupt(self):
        """Wake owned I/O without closing or reusing its descriptor."""
        self.fail(RemoteScienceError("Scientific communication was interrupted during retirement"))
        self.sock.shutdown(socket.SHUT_RDWR)


def _cleanup_observation(observation):
    require_type(observation, CleanupObservation)

    return observation


def _acknowledged(acknowledgement):
    require_type(acknowledgement, type(None), error_factory=ValueError)


async def _join_originals(tasks):
    if tasks:
        # Independent earlier failures retain their own owners. They do not
        # prevent this cleanup from making its own attempt.
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)


def _same_failure_evidence(first, second):
    if first.phase != second.phase:
        return False

    if first.cause is second.cause:
        return True

    first_evidence = decoded_error_evidence(first.cause)

    return (first_evidence is not None) and (first_evidence == decoded_error_evidence(second.cause))


class _ProcessCleanup(_CleanupState):
    """Start cleanup now; callers only observe this original retained action."""

    def __init__(self, host, action, *, seconds, before=()):
        super().__init__(seconds)
        self.action = action
        self.before = before
        self.task = _start_owned_task(self._run_cleanup)

        host.cleanups.add(self.task)
        self.task.add_done_callback(host.cleanups.discard)

    async def _run_cleanup(self):
        timer = self.loop.call_later(self.seconds, self._expire)

        try:
            await _join_originals(self.before)

            while True:
                observed = await self.action()
                self.settled = observed.settled
                self.expired = self.expired or observed.allowance_expired
                self.failures = observed.failures

                self.changed()

                if self.settled or self.failures:
                    return

                await asyncio.sleep(0.05)
        except BaseException as cause:
            self.failures = (*self.failures, Failure("cleanup", cause))

            self.changed()
        finally:
            timer.cancel()


class _ProcessOperation:
    def __init__(self, analysis, invocation, acknowledged):
        self.analysis = analysis
        self.invocation = invocation
        self.acknowledged = acknowledged
        self.reference = OperationRef(
            host=analysis.host.identity,
            analysis=analysis.identity,
            operation_id=invocation.operation_id,
        )

        self.remote_observation = None
        self.observation = None
        self.communication_failure = None
        self.stop_requested_at = None
        self.observation_task = None
        self.read_tasks = set()
        self.release = None
        self.released_result = None
        self.detached = False

    def _message(self, action, **extra):
        return {"action": action, "analysis": self.analysis.identity, "operation": self.reference.operation_id, **extra}

    def _snapshot(self):
        """Combine scientific facts with already known transport/native evidence.

        The child owns its scientific revision. This handle owns the published
        revision, including later stop, communication and native-retirement facts.
        Reconciliation performs no I/O and never retracts an observed failure.
        """
        host = self.analysis.host
        observed = self.remote_observation

        if observed is None:
            observed = Observation(
                operation=self.reference,
                target=self.invocation.target,
                revision=0,
                acceptance=AcceptanceState.OWNED if self.acknowledged else AcceptanceState.UNCERTAIN,
                dispatch=DispatchState.UNKNOWN,
                finished=False,
                outcome=None,
                result_state=ResultState.NONE,
                result=None,
                states=(),
                progress=(),
                progress_dropped=0,
                failures=(),
                stop_requested=False,
                allowance_expired=False,
                resources=ResourceState.UNKNOWN,
            )

        if (
            (self.communication_failure is None) and
            (not self.detached) and
            (host.channel is not None) and
            (host.channel.failure is not None)
        ):
            self.communication_failure = host._channel_failure(host.channel.failure)

        if self.communication_failure is not None:
            observed = replace(
                observed,
                failures=(*observed.failures, Failure("backend", self.communication_failure)),
                resources=ResourceState.RELEASED if observed.resources is ResourceState.RELEASED else ResourceState.UNKNOWN,
            )

        exited = host._worker_exited

        if exited:
            # The bytes lived only in this child. Its death proves their loss,
            # not a successful scientific disposer or a particular work outcome.
            observed = replace(
                observed,
                finished=True,
                result_state=ResultState.RELEASED if observed.result_state is ResultState.AVAILABLE else observed.result_state,
                resources=ResourceState.RELEASED if (host.retired or (observed.resources is ResourceState.RELEASED)) else ResourceState.UNKNOWN,
            )

        if (
            (self.stop_requested_at is not None) and
            ((observed.stop_requested_at is None) or (self.stop_requested_at < observed.stop_requested_at))
        ):
            observed = replace(observed, stop_requested=True, stop_requested_at=self.stop_requested_at)

        if self.observation is None:
            self.observation = observed
        elif (
            (replace(observed, revision=self.observation.revision) != self.observation) or
            (self.observation.revision < observed.revision)
        ):
            self.observation = replace(observed, revision=max(observed.revision, self.observation.revision + 1))

        return self.observation

    def _unavailable(self, cause):
        if self.communication_failure is None:
            self.communication_failure = self.analysis.host._channel_failure(cause)

        return self._snapshot()

    def _accept_observation(self, observed):
        require_type(observed, Observation)

        if not (observed.operation == self.reference):
            raise ValueError(f"{reprlib.repr(observed.operation)=} ≠ {self.reference=}")

        if not (observed.target == self.invocation.target):
            raise ValueError(f"{reprlib.repr(observed.target)=} ≠ {reprlib.repr(self.invocation.target)=}")

        return observed

    async def _peek_remote(self):
        if self.detached or self.analysis.host._worker_exited:
            return self._snapshot()

        observed = await self.analysis.host._exchange_async(
            self._message("observe"),
            accept=self._accept_observation,
        )

        # The original task publishes its evidence even when its caller stopped
        # waiting before the reply arrived.
        return self._publish(observed)

    def _publish(self, observed):
        if (self.remote_observation is None) or (self.remote_observation.revision <= observed.revision):
            if self.remote_observation is not None:
                # Scientific fields can advance before the child's completion
                # callback increments its revision. Accept those facts, while
                # retaining identical remote failures as their original owners.
                previous_failures = self.remote_observation.failures
                failures = []

                for (index, failure) in enumerate(observed.failures):
                    if (index < len(previous_failures)) and _same_failure_evidence(previous_failures[index], failure):
                        failure = previous_failures[index]

                    failures.append(failure)

                observed = replace(observed, failures=tuple(failures))

            self.remote_observation = observed

        return self._snapshot()

    def _barriers(self):
        return tuple(
            control.task
            for control in (self.analysis.cleanup, self.release)
            if control is not None
        )

    def _pending(self):
        return (*self.read_tasks, *((self.observation_task,) if self.observation_task is not None else ()))

    async def _observe_remote(self, before):
        await _join_originals(before)

        try:
            return await self._peek_remote()
        except (RemoteScienceError, LookupError) as cause:
            # Original I/O publishes failure even if its observer left first.
            observed = self._unavailable(cause)

            if not (observed.finished):
                raise self.communication_failure

            return observed

    async def observe(self, *, after, wait_seconds):
        require_running_loop(self.analysis.host.loop)
        require_nonnegative_number(wait_seconds)
        current = self._snapshot()

        if after is not None:
            require_type(after, int)

            if not (0 <= after <= current.revision):
                raise ValueError(f"Observation cursor {after=} should be nonnegative and not exceed {current.revision=}")

        if self.communication_failure is not None:
            if current.finished or (wait_seconds == 0):
                return current

            raise self.communication_failure

        if self.detached or self.analysis.host._worker_exited or (current.finished and (wait_seconds == 0)):
            return current

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds

        while True:
            if (self.observation_task is None) or self.observation_task.done():
                before = self._barriers()
                self.observation_task = _start_owned_task(lambda: self._observe_remote(before))

            # Return cached facts when the caller's wait is exhausted. The
            # original remote observation remains owned and will publish later.
            if (wait_seconds == 0) or ((after is not None) and (after != current.revision)):
                return current

            try:
                observed = await asyncio.wait_for(
                    asyncio.shield(self.observation_task),
                    timeout=max(0, deadline - loop.time()),
                )
            except TimeoutError:
                return self._snapshot()

            if (
                (after is None) or
                (after != observed.revision) or
                observed.finished or
                (loop.time() >= deadline)
            ):
                return observed

            current = observed

            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))

    def request_stop(self):
        require_running_loop(self.analysis.host.loop)

        if self.stop_requested_at is None:
            self.stop_requested_at = self.analysis.host.loop.time()

            if (not self.analysis.closed) and (not self._snapshot().finished):
                self.analysis.host._exchange(self._message("stop_operation"), accept=_acknowledged)

    async def read_result(self, result: ResultRef) -> bytes:
        require_running_loop(self.analysis.host.loop)
        self._require_result(result)

        require_type(self.release, type(None), error_factory=ValueError)

        before = self._barriers()

        async def read():
            await _join_originals(before)

            return await self.analysis.host._exchange_async(
                self._message("read_result", result=result),
                accept=self._accept_result,
            )

        task = _start_owned_task(read)

        self.read_tasks.add(task)
        task.add_done_callback(self.read_tasks.discard)

        return await asyncio.shield(task)

    def _accept_result(self, payload):
        require_type(payload, bytes)

        if not (len(payload) <= self.invocation.policy.reject_encoded_result_over_bytes):
            raise ValueError(f"{len(payload)=} should not exceed {self.invocation.policy.reject_encoded_result_over_bytes=}")

        return payload

    def _require_result(self, result_ref):
        require_type(result_ref, ResultRef)

        observed = self._snapshot()

        if not (result_ref.operation == self.reference):
            raise ValueError(f"{reprlib.repr(result_ref.operation)=} ≠ {self.reference=}")

        if not (result_ref == observed.result):
            raise ValueError(f"{reprlib.repr(result_ref)=} ≠ {reprlib.repr(observed.result)=}")

        if not (observed.finished):
            raise ValueError(f"{observed.finished=} should be True before using {reprlib.repr(result_ref)=}")

        if not (observed.result_state is ResultState.AVAILABLE):
            raise ValueError(f"{reprlib.repr(observed.result_state)=} should be {ResultState.AVAILABLE=}")

    def release_result(self, result: ResultRef):
        require_running_loop(self.analysis.host.loop)
        require_type(result, ResultRef)

        if self.release is None:
            self._require_result(result)

            if not (self.analysis.host.retiring is False):
                raise ValueError(f"{self.analysis.host.retiring=} assigns remaining result disposal to the host")

            before = (*self.analysis.host.exchanges, *self._pending(), *self._barriers())
            self.released_result = result
            self.release = _ProcessCleanup(
                self.analysis.host,
                lambda: self._release_remote(result),
                seconds=self.invocation.policy.cleanup_overdue_flag_seconds,
                before=before,
            )
        elif not (self.released_result == result):
            raise ValueError(f"{reprlib.repr(result)=} ≠ {reprlib.repr(self.released_result)=}")

        return self.release

    def _accept_release(self, release_observation):
        require_type(release_observation, dict, error_factory=ValueError)

        if not (set(release_observation) == (expected_release_fields := {"cleanup", "operation"})):
            raise ValueError(f"{reprlib.repr(set(release_observation))=} ≠ {expected_release_fields=}")

        _cleanup_observation(release_observation["cleanup"])
        self._accept_observation(release_observation["operation"])

        return release_observation

    async def _release_remote(self, result_ref):
        release_observation = await self.analysis.host._exchange_async(
            self._message("release_result", result=result_ref),
            accept=self._accept_release,
        )

        self._publish(release_observation["operation"])

        if release_observation["cleanup"].settled and self.analysis.forgotten:
            self.detached = True

        return release_observation["cleanup"]


class _ProcessAnalysis:
    def __init__(self, host):
        self.host = host
        self.identity = uuid4().hex
        self.reservation_ref = ReservationRef(
            host=host.identity,
            analysis=self.identity,
            allocation=host.hosted.allocation.allocation,
            binding_id=host.hosted.description.binding_id,
            binding_revision=host.hosted.description.binding_revision,
        )

        self.closed = False
        self.forgotten = False
        self.operations = {}
        self.cleanup = None

    def reservation(self, *, allocation: AllocationRef) -> ReservationRef:
        require_running_loop(self.host.loop)

        if not (allocation == self.host.hosted.allocation):
            raise ValueError(f"{reprlib.repr(allocation)=} ≠ {reprlib.repr(self.host.hosted.allocation)=}")

        return self.reservation_ref

    def submit(self, invocation: Invocation):
        require_running_loop(self.host.loop)
        require_type(invocation, Invocation)

        if self.closed or self.host.retiring:
            return NotAdmitted(AdmissionCode.CLOSED, f"Cannot submit with {self.closed=} or {self.host.retiring=}")

        if invocation.reservation != self.reservation_ref:
            return NotAdmitted(AdmissionCode.INVALID_REFERENCE, f"{reprlib.repr(invocation.reservation)=} ≠ {self.reservation_ref=}")

        if (
            (invocation.target.binding_id, invocation.target.binding_revision) !=
            (self.reservation_ref.binding_id, self.reservation_ref.binding_revision)
        ):
            return NotAdmitted(AdmissionCode.INVALID_REQUEST, f"Binding of {reprlib.repr(invocation.target)=} should match {self.reservation_ref=}")

        existing = self.operations.get(invocation.operation_id)

        if existing is not None:
            if not (existing.invocation == invocation):
                raise RequestConflict(f"{reprlib.repr(invocation.operation_id)=} already identifies another invocation")

            return existing

        if self.host._worker_exited:
            return NotAdmitted(AdmissionCode.CLOSED, f"{self.host._worker_exited=} prevents admitting {invocation.operation_id=}")

        largest_payload_byte_requirement = max(
            invocation.policy.reject_encoded_input_over_bytes,
            invocation.policy.reject_encoded_result_over_bytes,
            len(invocation.payload),
        )

        if not (largest_payload_byte_requirement <= MAX_PAYLOAD_BYTES):
            return NotAdmitted(AdmissionCode.UNSUPPORTED, f"{largest_payload_byte_requirement=} should not exceed {MAX_PAYLOAD_BYTES=}")

        request_frame = frame({"action": "submit", "invocation": invocation})
        operation = _ProcessOperation(self, invocation, acknowledged=False)
        self.operations[invocation.operation_id] = operation

        def accept(admission_response):
            if (type(admission_response) is dict) and (set(admission_response) == {"refusal"}):
                require_type(admission_response["refusal"], NotAdmitted)

                return admission_response["refusal"]

            require_type(admission_response, dict, error_factory=ValueError)

            if not (set(admission_response) == (expected_admission_fields := {"operation"})):
                raise ValueError(f"{reprlib.repr(set(admission_response))=} ≠ {expected_admission_fields=}")

            if not (admission_response["operation"] == operation.reference):
                raise ValueError(f"{reprlib.repr(admission_response['operation'])=} ≠ {operation.reference=}")

            return operation

        try:
            admitted_operation = self.host._exchange_frame(request_frame, accept=accept)
        except StartDeadlineExpired:
            # The worker raises this SDK outcome before creating an operation.
            # A received refusal proves non-admission; transport loss does not.
            del self.operations[invocation.operation_id]

            raise
        except BaseException as cause:
            operation.communication_failure = self.host._channel_failure(cause)

            if not isinstance(cause, Exception):
                raise
        else:
            if isinstance(admitted_operation, NotAdmitted):
                del self.operations[invocation.operation_id]

                return admitted_operation

            operation.acknowledged = True

        return operation

    def request_stop(self):
        require_running_loop(self.host.loop)

        if not self.closed:
            self.closed = True

            self.host._exchange({"action": "stop_analysis", "analysis": self.identity}, accept=_acknowledged)

    def close(self):
        require_running_loop(self.host.loop)

        if self.cleanup is None:
            self.closed = True
            seconds = min(
                (operation.invocation.policy.cleanup_overdue_flag_seconds for operation in self.operations.values()),
                default=self.host.retirement_seconds,
            )

            before = list(self.host.exchanges)

            for operation in self.operations.values():
                before.extend(operation._pending())

                if operation.release is not None:
                    before.append(operation.release.task)

            self.cleanup = _ProcessCleanup(self.host, self._close_remote, seconds=seconds, before=tuple(before))

        return self.cleanup

    async def _close_remote(self):
        observed = await self.host._exchange_async(
            {"action": "close_analysis", "analysis": self.identity},
            accept=_cleanup_observation,
        )

        if (not observed.settled) or observed.failures:
            return observed

        # Each reply is independently bounded. New public reads/releases wait
        # for this original task; earlier ones were joined before close began.
        for operation in self.operations.values():
            await operation._peek_remote()

        await self.host._exchange_async(
            {"action": "forget_analysis", "analysis": self.identity},
            accept=_acknowledged,
        )

        self.forgotten = True

        for operation in self.operations.values():
            operation.detached = operation._snapshot().result_state is not ResultState.AVAILABLE

        self.host._release_analysis(self)

        return observed


class ProcessExecutionHost:
    """Own science through one deployment-supplied native runner session.

    Deployment starts the existing runner with a fixed installed worker command
    and owner-session lifetime. This host owns its fresh session from construction;
    the native runner owns child termination, reaping and scratch cleanup.
    """

    # The child executes through the embedded host and uses its admission rules.
    capabilities = EMBEDDED_CAPABILITIES

    def __init__(
        self,
        *,
        session: RunnerSession,
        input_bytes: int,
        result_bytes: int,
        state_values: int,
        progress_records: int,
        initialization_seconds: float,
        retirement_seconds: float,
    ):
        for (capacity_name, capacity) in (
            ("input_bytes", input_bytes),
            ("result_bytes", result_bytes),
            ("state_values", state_values),
            ("progress_records", progress_records),
        ):
            _require_positive_capacity(capacity, name=capacity_name)

        require_positive_number(initialization_seconds)
        require_positive_number(retirement_seconds)

        require_type(session, RunnerSession)

        if not (session._connect_started is False):
            raise ValueError(f"{session._connect_started=} should be False before hosting science")

        self.host_options = dict(
            input_bytes=input_bytes,
            result_bytes=result_bytes,
            state_values=state_values,
            progress_records=progress_records,
            initialization_seconds=initialization_seconds,
            retirement_seconds=retirement_seconds,
        )

        self.initialization_seconds = initialization_seconds
        self.retirement_seconds = retirement_seconds
        self.loop = asyncio.get_running_loop()
        self.session = session
        self.native = None
        self.channel = None
        self.process_id = None
        self.start_task = None
        self.start_failure_observed = False
        self.hosted = None
        self.active_analysis = None
        self.retirement = None
        self.retiring = False
        self.retired = False
        self.draining = False
        self.scientific_retirement = None
        self.scientific_shutdown_task = None
        self.cleanups = set()
        self.exchanges = set()
        self.exchange_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="science-channel")

    async def __aenter__(self):
        require_running_loop(self.loop)

        return self

    async def __aexit__(self, exception_type, exception, traceback):
        try:
            await settle_cleanup(control=self.retire())
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    @staticmethod
    def _member_path(member) -> str:
        if not ("<locals>" not in member.__qualname__):
            raise ValueError(f"{reprlib.repr(member.__qualname__)=} should not contain '<locals>'")

        return f"{member.__module__}:{member.__qualname__}"  # machine-string: The installed-member resolver requires module:qualified_name without expression labels.

    async def start(
        self,
        *,
        contribution_type: type[Contribution],
        constructor: str,
        startup: Mapping,
        requirements: frozenset[ResourceRequirement],
    ):
        """Create the real model in a persistent child and retain its ownership."""

        require_running_loop(self.loop)

        require_type(self.start_task, type(None), error_factory=RuntimeError)

        if not (not self.retiring):
            raise RuntimeError(f"{self.retiring=} prevents starting another allocation")

        # Compile and validate before connecting: a control connection can
        # acquire a worker even if its greeting or scientific reply is lost.
        contribution_path = self._member_path(contribution_type)
        declaration = HostedContribution(
            contribution=None,
            contribution_type=contribution_type,
            host=uuid4().hex,
            allocation_name=uuid4().hex,
            requirements=requirements,
        )

        _require_supported_binding(declaration)
        require_type(constructor, str)

        (module, separator, member) = constructor.partition(":")

        if not (module and separator and member and ("<locals>" not in member)):
            raise ValueError(f"{reprlib.repr(constructor)=} should name a module:factory without '<locals>'")

        message = {
            "action": "start",
            "host_options": self.host_options,
            "constructor": constructor,
            "contribution_type": contribution_path,
            "startup": dict(startup),
            "requirements": tuple(requirements),
        }

        frame(message)
        deadline = time.monotonic() + self.initialization_seconds
        self.native = AsyncRunnerSession(self.session, setup_not_after=deadline)
        self.start_task = _start_owned_task(lambda: self._start_session(message, declaration, deadline))

        try:
            return await asyncio.shield(self.start_task)
        except BaseException as cause:
            if self.start_task.done() and not self.start_task.cancelled() and (self.start_task.exception() is cause):
                self.start_failure_observed = True

            raise

    async def _start_session(self, message, declaration, deadline):
        await self.native.connect(wait_seconds=self.initialization_seconds)
        self.channel = _Channel(self.session.science)

        def accept(ready):
            require_type(ready, dict, error_factory=ValueError)

            if not (set(ready) == (expected_readiness_fields := {"allocation", "binding_revision", "process_id"})):
                raise ValueError(f"{reprlib.repr(set(ready))=} ≠ {expected_readiness_fields=}")

            require_type(ready["allocation"], AllocationRef)
            require_type(ready["process_id"], int)

            if not (0 < ready["process_id"]):
                raise ValueError(f"{ready['process_id']=} should be positive")

            if not (ready["binding_revision"] == declaration.description.binding_revision):
                raise ValueError(f"{reprlib.repr(ready['binding_revision'])=} ≠ {declaration.description.binding_revision=}")

            return ready

        ready = await self._exchange_async(message, seconds=max(0, deadline - time.monotonic()), accept=accept)
        allocation = ready["allocation"]
        hosted = HostedContribution(
            contribution=None,
            contribution_type=declaration.contribution_type,
            host=allocation.host,
            allocation_name=allocation.allocation,
            requirements=declaration.requirements,
        )

        if not (hosted.allocation == allocation):
            raise RemoteScienceError(f"{reprlib.repr(hosted.allocation)=} ≠ {reprlib.repr(allocation)=}")

        if not (hosted.description.binding_revision == ready["binding_revision"]):
            raise RemoteScienceError(f"{hosted.description.binding_revision=} ≠ {ready['binding_revision']=}")

        self.hosted = hosted
        self.process_id = ready["process_id"]

        return hosted

    @property
    def identity(self):
        return self.hosted.allocation.host

    @property
    def allocations(self):
        return frozenset() if ((self.hosted is None) or self.retired) else frozenset({self.hosted.allocation})

    def _channel_failure(self, cause):
        if isinstance(cause, RemoteScienceError):
            failure = cause
        else:
            failure = RemoteScienceError("Scientific request has an uncertain outcome; retain its original owner")
            failure.__cause__ = cause

        if self.channel is not None:
            self.channel.fail(failure)

        return failure

    def _exchange_frame(self, request_frame, seconds=2, *, accept=None, deadline=None):
        if not (self.draining is False):
            raise RemoteScienceError(f"{self.draining=}; no request bytes were sent")

        if not (self.channel is not None):
            raise RemoteScienceError(f"{self.channel=} should be connected before sending a scientific request")

        if deadline is None:
            deadline = time.monotonic() + seconds

        return self.channel.exchange_frame(request_frame, deadline=deadline, accept=accept)

    def _exchange(self, message, seconds=2, *, accept=None):
        return self._exchange_frame(frame(message), seconds, accept=accept)

    async def _exchange_async(self, message, seconds=2, *, accept=None):
        require_running_loop(self.loop)

        if not (self.draining is False):
            raise RemoteScienceError(f"{self.draining=}; no request bytes were sent")

        request_frame = frame(message)
        deadline = time.monotonic() + seconds

        async def exchange():
            original = self.exchange_executor.submit(
                self._exchange_frame,
                request_frame,
                accept=accept,
                deadline=deadline,
            )

            return await await_original(original, deadline=None)

        task = _start_owned_task(exchange)

        self.exchanges.add(task)
        task.add_done_callback(self.exchanges.discard)

        return await asyncio.shield(task)

    # silent-defaults: allow-contract -- Ordinary analyses retain result handles after closure; typed calls release them
    def open_analysis(self, *, allocations: frozenset[AllocationRef], retain_results: bool = True):
        require_running_loop(self.loop)
        require_type(retain_results, bool)

        if not (not self.retiring):
            raise CapacityUnavailable(f"{self.retiring=} prevents opening another analysis")

        if not (self.hosted is not None):
            raise CapacityUnavailable(f"{self.hosted=} should identify a hosted contribution")

        if not (self.active_analysis is None):
            raise CapacityUnavailable(f"Analysis {self.active_analysis.identity=} still occupies this host")

        if not (self.channel is not None):
            raise CapacityUnavailable(f"{self.channel=} should be connected before opening an analysis")

        if not (self.channel.failure is None):
            raise CapacityUnavailable(f"Cannot open an analysis after {type(self.channel.failure)=}") from self.channel.failure

        if not (self.session.retirement is None):
            raise CapacityUnavailable(f"{reprlib.repr(self.session.retirement)=} should be None before opening an analysis")

        if not (self.session.original_exit is None):
            raise CapacityUnavailable(f"{self.session.original_exit=} prevents opening another analysis")

        if not (allocations == (expected_allocations := frozenset({self.hosted.allocation}))):
            raise ValueError(f"{reprlib.repr(allocations)=} ≠ {reprlib.repr(expected_allocations)=}")

        analysis = _ProcessAnalysis(self)
        self.active_analysis = analysis

        def accept(reservation):
            if not (reservation == analysis.reservation_ref):
                raise ValueError(f"{reprlib.repr(reservation)=} ≠ {analysis.reservation_ref=}")

            return reservation

        try:
            self._exchange({"action": "open_analysis", "analysis": analysis.identity, "retain_results": retain_results}, accept=accept)
        except BaseException as cause:
            # A possibly acquired remote reservation remains host-owned even
            # though the caller has not received its analysis handle.
            failure = self._channel_failure(cause)

            if not isinstance(cause, Exception):
                raise

            if not (failure is not cause):
                raise

            raise failure from cause

        return analysis

    @property
    def _worker_exited(self):
        return (self.session.original_exit is not None) or (self.session.retirement is not None)

    def _release_analysis(self, analysis):
        if self.active_analysis is analysis:
            self.active_analysis = None

    def retire_allocation(self, reference):
        require_running_loop(self.loop)

        if not (self.hosted is not None):
            raise ValueError(f"{self.hosted=} should identify the allocation being retired")

        if not (reference == self.hosted.allocation):
            raise ValueError(f"{reprlib.repr(reference)=} ≠ {reprlib.repr(self.hosted.allocation)=}")

        return self.retire()

    def retire(self):
        require_running_loop(self.loop)

        if self.retirement is None:
            # Graceful scientific shutdown can exit before native stop is needed
            self.session.mark_interruption()
            self.retiring = True
            self.retirement = _ProcessCleanup(self, self._shutdown, seconds=self.retirement_seconds)

        return self.retirement

    async def observe_exit(self, *, wait_seconds: float) -> OriginalWorkerExit | None:
        """Observe loss of this original worker without retiring a healthy host.

        Timeout and cancellation affect only this wait. The SDK retains the
        original monitor and its identity-correlated evidence. None denotes a
        final retirement receipt without exit observed before local interruption;
        it establishes neither exit cause nor cleanup authorization. The provider
        maps confirmed loss to its offering policy.
        """
        require_running_loop(self.loop)

        if self.native is None:
            raise RuntimeError(f"{self.native=} should own a started worker before observing exit")

        return await self.native.observe_exit(wait_seconds=wait_seconds)

    async def _shutdown(self):
        failures = []

        def record(phase, cause):
            if not any((failure.cause is cause) for failure in failures):
                failures.append(Failure(phase, cause))

        if self.start_task is None:
            self.draining = True

            self.session.close()
            self.exchange_executor.shutdown(wait=True)
            self.retired = True

            return CleanupObservation(0, True, False, ())

        # This is one scientific grace interval across startup, outstanding
        # handoffs and contribution cleanup. An expired wait never cancels the
        # original action. Native stop/join has the runner session's separate,
        # original aggregate allowance; it is never renewed after stop.
        science_deadline = time.monotonic() + self.retirement_seconds

        async def close_science():
            try:
                await asyncio.shield(self.start_task)
            except BaseException as cause:
                if not self.start_failure_observed:
                    record("initialization", cause)

            if self.channel is None:
                return

            if self.active_analysis is not None:
                control = self.active_analysis.close()

                await asyncio.shield(control.task)

                for failure in control.failures:
                    record(failure.phase, failure.cause)

            await _join_originals(tuple(task for task in self.cleanups if task is not self.retirement.task))
            observed = await self._exchange_async(
                {"action": "retire"},
                seconds=max(0, science_deadline - time.monotonic()),
                accept=_cleanup_observation,
            )

            self.scientific_retirement = observed

            for failure in observed.failures:
                record(failure.phase, failure.cause)

            if not (observed.settled):
                raise RemoteScienceError(f"{observed.settled=} should be True after scientific cleanup")

        self.scientific_shutdown_task = _start_owned_task(close_science)

        try:
            await asyncio.wait_for(
                asyncio.shield(self.scientific_shutdown_task),
                timeout=max(0, science_deadline - time.monotonic()),
            )
        except BaseException as cause:
            record("cleanup", cause)

        self.draining = True

        if (self.scientific_retirement is None) or (not self.scientific_retirement.settled):
            try:
                # Stop intent is retained even when the original connect has
                # not begun. A late setup must not acquire an unowned child.
                self.native.request_stop()
            except BaseException as cause:
                record("native_stop", cause)

            if self.channel is not None:
                try:
                    self.channel.interrupt()
                except OSError as cause:
                    record("channel_stop", cause)

        setup = self.native.setup
        no_worker = (
            (setup is None) or
            ((not self.session.control_connected) and isinstance(self.native.setup_failure, (RunnerStartupStopped, RunnerStartupUnavailable)))
        )

        if not no_worker:
            try:
                if (self.scientific_retirement is not None) and self.scientific_retirement.settled:
                    try:
                        # Leave half of this original native allowance for
                        # stop and join if interpreter exit itself stalls.
                        await self.native.observe_retirement(wait_seconds=self.session.retirement_seconds / 2)
                    except TimeoutError:
                        try:
                            self.native.request_stop()
                        except BaseException as cause:
                            record("native_stop", cause)

                        remaining_retirement_seconds = self.native.retirement_deadline - time.monotonic()

                        if not (0 < remaining_retirement_seconds):
                            raise TimeoutError(f"{self.native.retirement_deadline=} expired with {remaining_retirement_seconds=}")

                        await self.native.join_retirement(wait_seconds=remaining_retirement_seconds)
                else:
                    await self.native.observe_retirement(wait_seconds=self.session.retirement_seconds)
            except BaseException as cause:
                record("native_retirement", cause)

                try:
                    self.native.request_stop()
                except BaseException as stop:
                    record("native_stop", stop)

        # Both control readers retain their own fixed I/O deadlines. Join their
        # originals, including after caller timeout, before closing their FDs.
        for original in (self.native.setup, self.native.exit_reader, self.native.reader):
            if original is not None:
                try:
                    await await_original(original, deadline=None)
                except BaseException as cause:
                    if (
                        (original is self.native.setup) and
                        self.start_failure_observed and
                        self.start_task.done() and
                        not self.start_task.cancelled() and
                        (self.start_task.exception() is cause)
                    ):
                        continue

                    record("native_retirement", cause)

        if self.native.exit_failure is not None:
            record("native_exit_observation", self.native.exit_failure)

        await _join_originals((self.start_task, self.scientific_shutdown_task))
        await _join_originals(tuple(task for task in self.cleanups if task is not self.retirement.task))
        await _join_originals(tuple(self.exchanges))
        self.exchange_executor.shutdown(wait=True)

        receipt = self.session.retirement

        # Stop can prove non-acquisition while the retained setup is joining.
        # Classify its terminal evidence, not the earlier pending snapshot.
        no_worker = (
            (self.native.setup is None) or
            ((not self.session.control_connected) and isinstance(self.native.setup_failure, (RunnerStartupStopped, RunnerStartupUnavailable)))
        )

        self.retired = no_worker or ((receipt is not None) and receipt.scratch_released)

        if (receipt is not None) and (receipt.returncode != 0):
            record("worker", RemoteScienceError(f"Scientific worker exited with {receipt.returncode=}"))

        if not self.retired:
            record("native_retirement", RemoteScienceError("Scientific worker or scratch retirement remains unconfirmed"))

        try:
            self.session.close()
        except BaseException as cause:
            self.retired = False

            record("channel_cleanup", cause)

        if self.retired:
            self.active_analysis = None

        # Historical failures are evidence, not a claim that a confirmed-dead
        # worker still occupies its allocation. settle_cleanup reports failures
        # even when this resource settlement is independently confirmed.
        return CleanupObservation(0, self.retired, False, tuple(failures))
