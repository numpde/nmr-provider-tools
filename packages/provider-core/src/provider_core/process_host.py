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
            raise RemoteScienceError("Scientific channel was occupied through this exchange's deadline")

        try:
            if self.failure is not None:
                raise RemoteScienceError("Original scientific process channel is unavailable") from self.failure

            try:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    raise TimeoutError("Scientific exchange exceeded its original deadline before sending")

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


class _ProcessCleanup(_Changes):
    """Start cleanup now; callers only observe this original retained action."""

    def __init__(self, host, message, *, seconds, accept=None, on_settled=None):
        super().__init__()
        self.host = host
        self.message = message
        self.seconds = seconds
        self.accept = accept
        self.on_settled = on_settled
        self.settled = False
        self.expired = False
        self.failures = ()
        self.task = _start_owned_task(self._run)

    def _expire(self):
        self.expired = True
        self.changed()

    def _accept(self, value):
        observed = value if self.accept is None else self.accept(value)
        require_type(observed, CleanupObservation)
        return observed

    async def _run(self):
        timer = self.loop.call_later(self.seconds, self._expire)

        try:
            while True:
                observed = await self.host._exchange_async(self.message, accept=self._accept)
                self.settled = observed.settled
                self.expired = self.expired or observed.allowance_expired
                self.failures = observed.failures
                self.changed()

                if self.settled:
                    if self.on_settled is not None:
                        self.on_settled()

                    return

                if self.failures:
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
        self.release = None

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

    async def _observe_remote(self):
        observed = await self.analysis.host._exchange_async(
            self._message("observe"), accept=self._accept_observation,
        )
        # The original task publishes its evidence even when its caller stopped
        # waiting before the reply arrived.
        self.last_observation = observed
        self.failure_observation = None
        return observed

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
                    self.observation_task = _start_owned_task(self._observe_remote)

                observed = await asyncio.shield(self.observation_task)
            except (RemoteScienceError, LookupError) as cause:
                observed = self._unavailable(cause)

                if not observed.finished:
                    # The channel is gone, not the scientific owner. Give the
                    # workflow its original uncertainty instead of polling forever.
                    raise RemoteScienceError(
                        "Scientific operation may still be running in an unobservable worker"
                    ) from cause

            self.last_observation = observed

            if (after is None or after != observed.revision or observed.finished or
                    loop.time() >= deadline):
                return observed

            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))

    def request_stop(self):
        self.analysis.host._require_loop()

        if not self.stop_requested:
            self.stop_requested = True
            self.analysis.host._exchange(self._message("stop_operation"))

    async def read_result(self, result: ResultRef) -> bytes:
        self.analysis.host._require_loop()
        self._require_result(result)

        if self.release is not None:
            raise ValueError("Release of this exact result has already been authorized")

        return await self.analysis.host._exchange_async(self._message("read_result", result=result))

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

        if self.release is None:
            self._require_result(result)
            self.release = _ProcessCleanup(
                self.analysis.host,
                self._message("release_result", result=result),
                seconds=self.invocation.policy.cleanup_overdue_flag_seconds,
            )
        elif self.release.message["result"] != result:
            raise ValueError("Result release identifies another operation result")

        return self.release


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
        self.operations = {}
        self.cleanup = None

    def reservation(self, *, allocation: AllocationRef) -> ReservationRef:
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
        self.closed = True
        self.host._exchange({"action": "stop_analysis", "analysis": self.identity})

    def close(self):
        self.host._require_loop()

        if self.cleanup is None:
            self.closed = True
            seconds = min(
                (operation.invocation.policy.cleanup_overdue_flag_seconds for operation in self.operations.values()),
                default=self.host.retirement_seconds,
            )
            self.cleanup = _ProcessCleanup(
                self.host,
                {"action": "close_analysis", "analysis": self.identity},
                seconds=seconds,
                on_settled=lambda: self.host._release_analysis(self),
            )

        return self.cleanup


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
