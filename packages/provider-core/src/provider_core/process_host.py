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
import socket
import time
from threading import Lock
from uuid import uuid4

from provider_core._embedded_support import _Changes, _require_supported_binding
from provider_core.async_runner_session import AsyncRunnerSession, await_original
from provider_core.contribution import ConcurrencyRequirement, InterruptionSupport, IsolationRequirement, ResourceRequirement
from provider_core.hosting import (
    AcceptanceState, AdmissionCode, AllocationRef, BackendCapabilities,
    CapacityUnavailable, CleanupObservation, DispatchState, Failure, Invocation,
    NotAdmitted, Observation, OperationRef,
    RequestConflict, ReservationRef, ResourceState, ResultRef, ResultState,
    _raise_context_failure, _start_owned_task, settle_cleanup,
)
from provider_core.process_wire import MAX_PAYLOAD_BYTES, RemoteScienceError, frame, receive
from provider_core.runner_session import RunnerSession, RunnerStartupStopped, RunnerStartupUnavailable
from provider_core.scientific import Contribution, HostedContribution
from provider_core.tools import require_nonnegative_number, require_positive_number
from provider_core.type_requirements import require_type


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
        request = frame(message)
        return self.exchange_frame(request, deadline=time.monotonic() + seconds, accept=accept)

    def exchange_frame(self, request, *, deadline, accept=None):
        remaining = max(0, deadline - time.monotonic())

        if not self.lock.acquire(timeout=remaining):
            raise TimeoutError("Scientific channel was occupied; this exchange sent no bytes")

        try:
            if self.failure is not None:
                raise RemoteScienceError("Original scientific process channel is unavailable") from self.failure

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise TimeoutError("Scientific exchange exceeded its original deadline before sending")

            try:
                self.sock.settimeout(remaining)
                self.sock.sendall(request)
                reply = receive(self.sock, deadline=deadline)

                if type(reply) is not dict or type(reply.get("ok")) is not bool:
                    raise ValueError("Scientific process replied with an unexpected envelope")

                if reply["ok"]:
                    if set(reply) != {"ok", "value"}:
                        raise ValueError("Scientific process success envelope has unexpected fields")

                    return reply["value"] if accept is None else accept(reply["value"])

                if set(reply) != {"ok", "error"} or not isinstance(reply["error"], BaseException):
                    raise ValueError("Scientific process failure envelope has no failure evidence")
            except BaseException as cause:
                failure = self.fail(cause)

                if isinstance(cause, Exception):
                    raise failure from cause

                raise

            # A well-formed remote failure is evidence, not a corrupt frame.
            # Mutating callers decide whether their request is now uncertain.
            if not reply["ok"]:
                raise reply["error"]
        finally:
            self.lock.release()

    def close(self):
        self.sock.close()

    def interrupt(self):
        """Wake owned I/O without closing or reusing its descriptor."""
        self.fail(RemoteScienceError("Scientific communication was interrupted during retirement"))
        self.sock.shutdown(socket.SHUT_RDWR)


def _cleanup_observation(value):
    require_type(value, CleanupObservation)
    return value


def _acknowledged(value):
    if value is not None:
        raise ValueError("Scientific control acknowledgement has unexpected content")


async def _join_originals(tasks):
    if tasks:
        # Independent earlier failures retain their own owners. They do not
        # prevent this cleanup from making its own attempt.
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)


class _ProcessCleanup(_Changes):
    """Start cleanup now; callers only observe this original retained action."""

    def __init__(self, host, action, *, seconds, before=()):
        super().__init__()
        self.action = action
        self.seconds = seconds
        self.before = before
        self.settled = False
        self.expired = False
        self.failures = ()
        self.task = _start_owned_task(self._run)
        host.cleanups.add(self.task)
        self.task.add_done_callback(host.cleanups.discard)

    def _expire(self):
        self.expired = True
        self.changed()

    async def _run(self):
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

    async def observe(self, *, after, wait_seconds):
        await self.wait(after, wait_seconds)
        return CleanupObservation(self.revision, self.settled, self.expired, self.failures)


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
        self.last_observation = None
        self.failure_observation = None
        self.submission_failure = None
        self.stop_requested = False
        self.observation_task = None
        self.read_tasks = set()
        self.release = None
        self.released_result = None
        self.detached = False

    def _message(self, action, **extra):
        return {"action": action, "analysis": self.analysis.identity, "operation": self.reference.operation_id, **extra}

    def _unavailable(self, cause):
        host = self.analysis.host
        exited = host.session.retirement is not None

        if self.failure_observation is not None:
            if (exited and not self.failure_observation.finished) or (host.retired and self.failure_observation.resources is not ResourceState.RELEASED):
                self.failure_observation = replace(
                    self.failure_observation,
                    revision=self.failure_observation.revision + 1,
                    finished=True,
                    resources=ResourceState.RELEASED if host.retired else ResourceState.UNKNOWN,
                )

            return self.failure_observation

        observed = self.last_observation or Observation(
            operation=self.reference,
            target=self.invocation.target,
            revision=0,
            acceptance=AcceptanceState.OWNED if self.acknowledged else AcceptanceState.UNCERTAIN,
            dispatch=DispatchState.UNKNOWN,
            finished=exited,
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
        observed = replace(
            observed,
            revision=observed.revision + 1,
            finished=observed.finished or exited,
            failures=(*observed.failures, Failure("backend", cause)),
            stop_requested=observed.stop_requested or self.stop_requested,
            resources=(ResourceState.RELEASED if host.retired or observed.resources is ResourceState.RELEASED else ResourceState.UNKNOWN),
        )
        self.failure_observation = observed
        return observed

    def _accept_observation(self, observed):
        require_type(observed, Observation)

        if observed.operation != self.reference or observed.target != self.invocation.target:
            raise ValueError("Scientific worker observation identifies different work")

        return observed

    async def _peek_remote(self):
        if self.detached:
            return self.last_observation

        observed = await self.analysis.host._exchange_async(
            self._message("observe"), accept=self._accept_observation,
        )
        # The original task publishes its evidence even when its caller stopped
        # waiting before the reply arrived.
        return self._publish(observed)

    def _publish(self, observed):
        if self.last_observation is None or observed.revision >= self.last_observation.revision:
            self.last_observation = observed
            self.failure_observation = None

        return self.last_observation

    def _barriers(self):
        return tuple(
            control.task for control in (self.analysis.cleanup, self.release)
            if control is not None
        )

    def _pending(self):
        return (*self.read_tasks, *((self.observation_task,) if self.observation_task is not None else ()))

    async def _observe_remote(self, before):
        await _join_originals(before)
        return await self._peek_remote()

    async def observe(self, *, after, wait_seconds):
        self.analysis.host._require_loop()
        require_nonnegative_number(wait_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds

        while True:
            try:
                if self.submission_failure is not None:
                    raise self.submission_failure

                if self.observation_task is None or self.observation_task.done():
                    before = self._barriers()
                    self.observation_task = _start_owned_task(lambda: self._observe_remote(before))

                observed = await asyncio.shield(self.observation_task)
            except (RemoteScienceError, LookupError) as cause:
                observed = self._unavailable(cause)

                if not observed.finished:
                    # The channel is gone, not the scientific owner. Give the
                    # workflow its original uncertainty instead of polling forever.
                    raise RemoteScienceError(
                        "Scientific operation may still be running in an unobservable worker"
                    ) from cause

            if (after is None or after != observed.revision or observed.finished or
                    loop.time() >= deadline):
                return observed

            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))

    def request_stop(self):
        self.analysis.host._require_loop()

        if not self.stop_requested:
            self.stop_requested = True

            if not self.analysis.closed and not (self.last_observation is not None and self.last_observation.finished):
                self.analysis.host._exchange(self._message("stop_operation"), accept=_acknowledged)

    async def read_result(self, result: ResultRef) -> bytes:
        self.analysis.host._require_loop()
        self._require_result(result)

        if self.release is not None:
            raise ValueError("Release of this exact result has already been authorized")

        before = self._barriers()

        async def read():
            await _join_originals(before)
            return await self.analysis.host._exchange_async(
                self._message("read_result", result=result), accept=self._accept_result,
            )

        task = _start_owned_task(read)
        self.read_tasks.add(task)
        task.add_done_callback(self.read_tasks.discard)
        return await asyncio.shield(task)

    def _accept_result(self, payload):
        require_type(payload, bytes)

        if len(payload) > self.invocation.policy.reject_encoded_result_over_bytes:
            raise ValueError("Scientific worker result exceeds its admitted allowance")

        return payload

    def _require_result(self, result):
        require_type(result, ResultRef)
        observed = self.last_observation

        if (
            result.operation != self.reference or observed is None or
            result != observed.result or not observed.finished or
            observed.result_state is not ResultState.AVAILABLE
        ):
            raise ValueError("Result reference does not name this operation's available result")

    def release_result(self, result: ResultRef):
        self.analysis.host._require_loop()
        require_type(result, ResultRef)

        if self.release is None:
            self._require_result(result)

            if self.analysis.host.retiring:
                raise ValueError("Host retirement already owns disposal of remaining results")

            before = (*self.analysis.host.exchanges, *self._pending(), *self._barriers())
            self.released_result = result
            self.release = _ProcessCleanup(
                self.analysis.host,
                lambda: self._release_remote(result),
                seconds=self.invocation.policy.cleanup_overdue_flag_seconds,
                before=before,
            )
        elif self.released_result != result:
            raise ValueError("Result release identifies another operation result")

        return self.release

    def _accept_release(self, value):
        if type(value) is not dict or set(value) != {"cleanup", "operation"}:
            raise ValueError("Scientific result release has an unexpected reply")

        _cleanup_observation(value["cleanup"])
        self._accept_observation(value["operation"])
        return value

    async def _release_remote(self, result):
        reply = await self.analysis.host._exchange_async(
            self._message("release_result", result=result), accept=self._accept_release,
        )
        self._publish(reply["operation"])

        if reply["cleanup"].settled and self.analysis.forgotten:
            self.detached = True

        return reply["cleanup"]


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
        self.host._require_loop()
        if allocation != self.host.hosted.allocation:
            raise ValueError("This analysis does not own the specified allocation")

        return self.reservation_ref

    def submit(self, invocation: Invocation):
        self.host._require_loop()
        require_type(invocation, Invocation)

        if self.closed or self.host.retiring:
            return NotAdmitted(AdmissionCode.CLOSED, "Scientific analysis is closing")

        if invocation.reservation != self.reservation_ref:
            return NotAdmitted(AdmissionCode.INVALID_REFERENCE, "Scientific reservation differs from this analysis")

        if ((invocation.target.binding_id, invocation.target.binding_revision) !=
                (self.reservation_ref.binding_id, self.reservation_ref.binding_revision)):
            return NotAdmitted(AdmissionCode.INVALID_REQUEST, "Scientific call does not belong to this allocation")

        existing = self.operations.get(invocation.operation_id)

        if existing is not None:
            if existing.invocation != invocation:
                raise RequestConflict("Scientific operation identity already refers to different work")

            return existing

        if max(
            invocation.policy.reject_encoded_input_over_bytes,
            invocation.policy.reject_encoded_result_over_bytes,
            len(invocation.payload),
        ) > MAX_PAYLOAD_BYTES:
            return NotAdmitted(AdmissionCode.UNSUPPORTED, "Scientific process byte allowance exceeds its supported payload limit")

        request = frame({"action": "submit", "invocation": invocation})
        operation = _ProcessOperation(self, invocation, acknowledged=False)
        self.operations[invocation.operation_id] = operation

        def accept(outcome):
            if type(outcome) is dict and set(outcome) == {"refusal"}:
                require_type(outcome["refusal"], NotAdmitted)
                return outcome["refusal"]

            if type(outcome) is not dict or set(outcome) != {"operation"} or outcome["operation"] != operation.reference:
                raise ValueError("Scientific worker admitted another operation identity")

            return operation

        try:
            outcome = self.host._exchange_frame(request, accept=accept)
        except BaseException as cause:
            operation.submission_failure = self.host._channel_failure(cause)

            if not isinstance(cause, Exception):
                raise
        else:
            if isinstance(outcome, NotAdmitted):
                del self.operations[invocation.operation_id]
                return outcome

            operation.acknowledged = True

        return operation

    def request_stop(self):
        self.host._require_loop()

        if not self.closed:
            self.closed = True
            self.host._exchange({"action": "stop_analysis", "analysis": self.identity}, accept=_acknowledged)

    def close(self):
        self.host._require_loop()

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

        if not observed.settled or observed.failures:
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
            operation.detached = operation.last_observation.result_state is not ResultState.AVAILABLE

        self.host._release_analysis(self)
        return observed


class ProcessExecutionHost:
    """Own science through one deployment-supplied native runner session.

    Deployment starts the existing runner with a fixed installed worker command
    and owner-session lifetime. This host owns its fresh session from construction;
    the native runner owns child termination, reaping and scratch cleanup.
    """

    capabilities = BackendCapabilities(
        contract_versions=frozenset({1}),
        isolation=frozenset({IsolationRequirement.SHARED_PROCESS}),
        concurrency=frozenset({ConcurrencyRequirement.INSTANCE_SERIAL, ConcurrencyRequirement.PARALLEL}),
        interruption=frozenset({InterruptionSupport.NONE, InterruptionSupport.COOPERATIVE}),
        progress=True,
    )

    def __init__(
        self, *, session: RunnerSession, input_bytes: int, result_bytes: int, state_values: int,
        progress_records: int, initialization_seconds: float, retirement_seconds: float,
    ):
        for value in (input_bytes, result_bytes, state_values, progress_records):
            require_type(value, int)

            if value <= 0:
                raise ValueError("Scientific process capacities must be positive integers")

        for value in (initialization_seconds, retirement_seconds):
            require_positive_number(value)

        require_type(session, RunnerSession)

        if session._connect_started:
            raise ValueError("Scientific hosting requires a fresh native runner session")

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

    def _require_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Control this scientific host on its original event loop")

    async def __aenter__(self):
        self._require_loop()
        return self

    async def __aexit__(self, exception_type, exception, traceback):
        try:
            await settle_cleanup(control=self.retire())
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    @staticmethod
    def _member_path(member) -> str:
        if "<locals>" in member.__qualname__:
            raise ValueError("Scientific process requires an installed top-level class or factory")

        return f"{member.__module__}:{member.__qualname__}"

    async def start(self, *, contribution_type: type[Contribution], constructor: str,
                    startup: Mapping, requirements: frozenset[ResourceRequirement]):
        """Create the real model in a persistent child and retain its ownership."""
        self._require_loop()
        if self.start_task is not None or self.retiring:
            raise RuntimeError("This process host accepts exactly one allocation")

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
        module, separator, member = constructor.partition(":")

        if not module or not separator or not member or "<locals>" in member:
            raise ValueError("Scientific process requires an installed module:factory path")

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
            if self.start_task.done() and not self.start_task.cancelled() and self.start_task.exception() is cause:
                self.start_failure_observed = True

            raise

    async def _start_session(self, message, declaration, deadline):
        await self.native.connect(wait_seconds=self.initialization_seconds)
        self.channel = _Channel(self.session.science)

        def accept(ready):
            if type(ready) is not dict or set(ready) != {"allocation", "binding_revision", "process_id"}:
                raise ValueError("Scientific worker readiness has unexpected fields")

            require_type(ready["allocation"], AllocationRef)
            require_type(ready["process_id"], int)

            if ready["process_id"] <= 0 or ready["binding_revision"] != declaration.description.binding_revision:
                raise ValueError("Scientific worker readiness differs from the selected declaration")

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

        if hosted.allocation != allocation or hosted.description.binding_revision != ready["binding_revision"]:
            raise RemoteScienceError("Parent and child scientific declaration revisions differ")

        self.hosted = hosted
        self.process_id = ready["process_id"]
        return hosted

    @property
    def identity(self):
        return self.hosted.allocation.host

    @property
    def allocations(self):
        return frozenset() if self.hosted is None or self.retired else frozenset({self.hosted.allocation})

    def _channel_failure(self, cause):
        if isinstance(cause, RemoteScienceError):
            failure = cause
        else:
            failure = RemoteScienceError("Scientific request has an uncertain outcome; retain its original owner")
            failure.__cause__ = cause

        if self.channel is not None:
            self.channel.fail(failure)

        return failure

    def _exchange_frame(self, request, seconds=2, *, accept=None, deadline=None):
        if self.draining:
            raise RemoteScienceError("Scientific communication is retiring; this request was not sent")

        if self.channel is None:
            raise RemoteScienceError("No scientific process is connected")

        if deadline is None:
            deadline = time.monotonic() + seconds

        return self.channel.exchange_frame(request, deadline=deadline, accept=accept)

    def _exchange(self, message, seconds=2, *, accept=None):
        return self._exchange_frame(frame(message), seconds, accept=accept)

    async def _exchange_async(self, message, seconds=2, *, accept=None):
        self._require_loop()

        if self.draining:
            raise RemoteScienceError("Scientific communication is retiring; this request was not sent")

        request = frame(message)
        deadline = time.monotonic() + seconds

        async def exchange():
            original = self.exchange_executor.submit(
                self._exchange_frame, request, accept=accept, deadline=deadline,
            )
            return await await_original(original, deadline=None)

        task = _start_owned_task(exchange)
        self.exchanges.add(task)
        task.add_done_callback(self.exchanges.discard)
        return await asyncio.shield(task)

    def open_analysis(self, *, allocations: frozenset[AllocationRef]):
        self._require_loop()
        if (
            self.retiring or
            self.hosted is None or
            self.active_analysis is not None or
            self.channel is None or
            self.channel.failure is not None or
            self.session.retirement is not None
        ):
            raise CapacityUnavailable("The scientific process allocation is unavailable or occupied")

        if allocations != frozenset({self.hosted.allocation}):
            raise ValueError("Process analysis requires its exact hosted allocation")

        analysis = _ProcessAnalysis(self)
        self.active_analysis = analysis

        def accept(reservation):
            if reservation != analysis.reservation_ref:
                raise ValueError("Scientific worker opened a different analysis reservation")

            return reservation

        try:
            self._exchange({"action": "open_analysis", "analysis": analysis.identity}, accept=accept)
        except BaseException as cause:
            # A possibly acquired remote reservation remains host-owned even
            # though the caller has not received its analysis handle.
            failure = self._channel_failure(cause)

            if isinstance(cause, Exception):
                if failure is cause:
                    raise

                raise failure from cause

            raise

        return analysis

    def _release_analysis(self, analysis):
        if self.active_analysis is analysis:
            self.active_analysis = None

    def retire_allocation(self, reference):
        self._require_loop()
        if self.hosted is None or reference != self.hosted.allocation:
            raise ValueError("Process retirement requires the current allocation reference")

        return self.retire()

    def retire(self):
        self._require_loop()
        if self.retirement is None:
            self.retiring = True
            self.retirement = _ProcessCleanup(self, self._shutdown, seconds=self.retirement_seconds)

        return self.retirement

    async def _shutdown(self):
        failures = []

        def record(phase, cause):
            if not any(item.cause is cause for item in failures):
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

            if not observed.settled:
                raise RemoteScienceError("Scientific cleanup did not confirm settlement")

        self.scientific_shutdown_task = _start_owned_task(close_science)

        try:
            await asyncio.wait_for(
                asyncio.shield(self.scientific_shutdown_task),
                timeout=max(0, science_deadline - time.monotonic()),
            )
        except BaseException as cause:
            record("cleanup", cause)

        self.draining = True
        if self.scientific_retirement is None or not self.scientific_retirement.settled:
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
            setup is None or
            (not self.session.control_connected and isinstance(self.native.setup_failure, (RunnerStartupStopped, RunnerStartupUnavailable)))
        )

        if not no_worker:
            try:
                if self.scientific_retirement is not None and self.scientific_retirement.settled:
                    try:
                        # Leave half of this original native allowance for
                        # stop and join if interpreter exit itself stalls.
                        await self.native.observe_retirement(wait_seconds=self.session.retirement_seconds / 2)
                    except TimeoutError:
                        try:
                            self.native.request_stop()
                        except BaseException as cause:
                            record("native_stop", cause)

                        remaining = self.native.retirement_deadline - time.monotonic()

                        if remaining <= 0:
                            raise TimeoutError("Native scientific retirement exceeded its original allowance")

                        await self.native.join_retirement(wait_seconds=remaining)
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
        for original in (self.native.setup, self.native.reader):
            if original is not None:
                try:
                    await await_original(original, deadline=None)
                except BaseException as cause:
                    if (
                        original is self.native.setup and self.start_failure_observed and
                        self.start_task.done() and not self.start_task.cancelled() and
                        self.start_task.exception() is cause
                    ):
                        continue

                    record("native_retirement", cause)

        await _join_originals((self.start_task, self.scientific_shutdown_task))
        await _join_originals(tuple(task for task in self.cleanups if task is not self.retirement.task))
        await _join_originals(tuple(self.exchanges))
        self.exchange_executor.shutdown(wait=True)

        receipt = self.session.retirement
        # Stop can prove non-acquisition while the retained setup is joining.
        # Classify its terminal evidence, not the earlier pending snapshot.
        no_worker = (
            self.native.setup is None or
            (not self.session.control_connected and isinstance(self.native.setup_failure, (RunnerStartupStopped, RunnerStartupUnavailable)))
        )
        self.retired = no_worker or (receipt is not None and receipt.scratch_released)

        if receipt is not None and receipt.returncode != 0:
            record("worker", RemoteScienceError(f"Scientific worker exited with returncode={receipt.returncode}"))

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
