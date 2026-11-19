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
import os
import socket
import subprocess
import sys
import tempfile
import time
from threading import Lock
from uuid import uuid4

from provider_core._embedded_support import _Changes, _Cleanup
from provider_core.async_runner_session import await_original
from provider_core.contribution import ConcurrencyRequirement, InterruptionSupport, IsolationRequirement, ResourceRequirement
from provider_core.hosting import (
    AcceptanceState, AdmissionCode, AllocationRef, BackendCapabilities,
    CapacityUnavailable, CleanupObservation, DispatchState, Failure, Invocation,
    NotAdmitted, Observation, OperationPolicy, OperationRef, Outcome,
    RequestConflict, ReservationRef, ResourceState, ResultRef, ResultState,
    _raise_context_failure, _start_owned_task, settle_cleanup,
)
from provider_core.process_wire import MAX_PAYLOAD_BYTES, RemoteScienceError, frame, receive
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

    def __init__(self, action, *, seconds, before=()):
        super().__init__()
        self.action = action
        self.seconds = seconds
        self.before = before
        self.settled = False
        self.expired = False
        self.failures = ()
        self.task = _start_owned_task(self._run)

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
        exited = host.process is not None and host.process.poll() is not None

        if self.failure_observation is not None:
            if exited and not self.failure_observation.finished:
                self.failure_observation = replace(
                    self.failure_observation,
                    revision=self.failure_observation.revision + 1,
                    finished=True,
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
            resources=(ResourceState.RELEASED if observed.resources is ResourceState.RELEASED else ResourceState.UNKNOWN),
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
            before = (*self.analysis.host.exchanges, *self._pending(), *self._barriers())
            self.released_result = result
            self.release = _ProcessCleanup(
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

            self.cleanup = _ProcessCleanup(self._close_remote, seconds=seconds, before=tuple(before))

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
    """Own one installed scientific child, with no automatic replay or restart."""

    capabilities = BackendCapabilities(
        contract_versions=frozenset({1}),
        isolation=frozenset({IsolationRequirement.SHARED_PROCESS}),
        concurrency=frozenset({ConcurrencyRequirement.INSTANCE_SERIAL, ConcurrencyRequirement.PARALLEL}),
        interruption=frozenset({InterruptionSupport.NONE, InterruptionSupport.COOPERATIVE}),
        progress=True,
    )

    def __init__(
        self, *, input_bytes: int, result_bytes: int, state_values: int,
        progress_records: int, initialization_seconds: float, retirement_seconds: float,
    ):
        for value in (input_bytes, result_bytes, state_values, progress_records):
            require_type(value, int)

            if value <= 0:
                raise ValueError("Scientific process capacities must be positive integers")

        for value in (initialization_seconds, retirement_seconds):
            require_positive_number(value)

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
        self.channel = None
        self.process = None
        self.stderr = None
        self.start_task = None
        self.hosted = None
        self.active_analysis = None
        self.retirement = None
        self.retiring = False
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
        if self.process is not None or self.retiring:
            raise RuntimeError("This process host accepts exactly one allocation")

        # Reject nonportable declarations and startup data before spawning a
        # child or assigning any operating-system resources.
        message = {
            "action": "start",
            "host_options": self.host_options,
            "constructor": constructor,
            "contribution_type": self._member_path(contribution_type),
            "startup": dict(startup),
            "requirements": tuple(requirements),
        }
        frame(message)

        parent, child = socket.socketpair()
        self.stderr = tempfile.TemporaryFile()

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PROVIDER_CORE_IN_CONTAINER": "1",
            "NMR_PROVIDER_TOOLS_IN_CONTAINER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-m", "provider_core.process_worker", "--session-fd", str(child.fileno())],
                pass_fds=(child.fileno(),),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self.stderr,
                env=environment,
            )
        except BaseException:
            parent.close()
            self.stderr.close()
            raise
        finally:
            child.close()

        self.channel = _Channel(parent)
        self.start_task = _start_owned_task(lambda: self._exchange_async(message, seconds=self.initialization_seconds + 5))
        ready = await asyncio.shield(self.start_task)
        allocation = ready["allocation"]
        hosted = HostedContribution(
            contribution=None,
            contribution_type=contribution_type,
            host=allocation.host,
            allocation_name=allocation.allocation,
            requirements=requirements,
        )

        if hosted.allocation != allocation or hosted.description.binding_revision != ready["binding_revision"]:
            raise RemoteScienceError("Parent and child scientific declaration revisions differ")

        self.hosted = hosted
        return hosted

    @property
    def identity(self):
        return self.hosted.allocation.host

    @property
    def allocations(self):
        return frozenset() if self.hosted is None else frozenset({self.hosted.allocation})

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
        if self.channel is None:
            raise RemoteScienceError("No scientific process is connected")

        if deadline is None:
            deadline = time.monotonic() + seconds

        return self.channel.exchange_frame(request, deadline=deadline, accept=accept)

    def _exchange(self, message, seconds=2, *, accept=None):
        return self._exchange_frame(frame(message), seconds, accept=accept)

    async def _exchange_async(self, message, seconds=2, *, accept=None):
        self._require_loop()
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
            self.process is None or
            self.process.poll() is not None
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
            self.retirement = _Cleanup(self.retirement_seconds)
            self.retirement.start(self._shutdown)

        return self.retirement

    async def _shutdown(self):
        failures = []

        if self.process is None:
            self.exchange_executor.shutdown(wait=False)
            return

        if self.start_task is not None and not self.start_task.done():
            self.process.terminate()

            try:
                await asyncio.wait_for(asyncio.shield(self.start_task), timeout=self.retirement_seconds)
            except BaseException as cause:
                failures.append(cause)

        if self.channel is not None and self.channel.failure is None and not failures:
            try:
                observed = await self._exchange_async({"action": "retire"}, seconds=self.retirement_seconds)

                if not observed.settled:
                    raise RemoteScienceError("Scientific cleanup was not confirmed")
            except BaseException as cause:
                failures.append(cause)
        elif self.channel is not None and self.channel.failure is not None:
            failures.append(RemoteScienceError("Scientific cleanup could not be confirmed after channel failure"))

        try:
            returncode = await asyncio.to_thread(self.process.wait, timeout=self.retirement_seconds)
        except subprocess.TimeoutExpired:
            self.process.terminate()

            try:
                await asyncio.to_thread(self.process.wait, timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)

            failures.append(RemoteScienceError("Scientific worker required forced termination; cleanup is unconfirmed"))
        else:
            if returncode != 0:
                failures.append(RemoteScienceError(f"Scientific worker exited with {returncode=}"))
        finally:
            if self.exchanges:
                await asyncio.gather(*(asyncio.shield(task) for task in tuple(self.exchanges)), return_exceptions=True)

            self.exchange_executor.shutdown(wait=False)

            if self.channel is not None:
                self.channel.close()

            if self.stderr is not None:
                self.stderr.close()

        if failures:
            raise BaseExceptionGroup("Scientific worker retirement left unresolved evidence", failures)
