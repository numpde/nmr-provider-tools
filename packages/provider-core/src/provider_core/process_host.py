"""One installed scientific process per allocation, with retained SDK owners.

The application uses the existing AnalysisExecution/Operation contracts. Worker
loss is never non-admission or permission to run the original science again.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Mapping
import os
import socket
import subprocess
import sys
import tempfile
from threading import Lock
from uuid import uuid4

from provider_core._embedded_support import _Cleanup
from provider_core.contribution import ConcurrencyRequirement, InterruptionSupport, IsolationRequirement, ResourceRequirement
from provider_core.hosting import (
    AcceptanceState, AdmissionCode, AllocationRef, BackendCapabilities,
    CapacityUnavailable, CleanupObservation, DispatchState, Failure, Invocation,
    NotAdmitted, Observation, OperationPolicy, OperationRef, Outcome,
    RequestConflict, ReservationRef, ResourceState, ResultRef, ResultState,
    _raise_context_failure, settle_cleanup,
)
from provider_core.process_wire import RemoteScienceError, frame, receive
from provider_core.scientific import Contribution, HostedContribution


class _Channel:
    """One bounded request/reply at a time; a lost reply retires the channel."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = Lock()
        self.failure = None

    def exchange(self, message, seconds=2):
        with self.lock:
            if self.failure is not None:
                raise RemoteScienceError("Original scientific process channel is unavailable") from self.failure

            # Framing failure is a local refusal before any send: it must not
            # turn the channel or a scientific operation into an uncertainty.
            request = frame(message)

            try:
                self.sock.settimeout(seconds)
                self.sock.sendall(request)
                reply = receive(self.sock)
            except (OSError, EOFError, ValueError, TypeError) as cause:
                self.failure = cause
                self.sock.close()
                raise RemoteScienceError("Scientific process reply is unknown; do not repeat its work") from cause

            if not isinstance(reply, dict) or set(reply) != {"ok", "value"} and set(reply) != {"ok", "error"}:
                self.failure = ValueError("Scientific process replied with an unexpected envelope")
                self.sock.close()
                raise RemoteScienceError("Scientific process reply is not usable") from self.failure

            if reply["ok"] is not True:
                raise reply["error"]

            return reply["value"]

    def close(self):
        self.sock.close()


class _ProcessCleanup:
    """Retain one remote cleanup request; re-observation never starts new work."""

    def __init__(self, host, message, settled=None):
        self.host = host
        self.message = message
        self.terminal = settled
        self.revision = 0
        self.on_settled = None

    async def observe(self, *, after, wait_seconds):
        if self.terminal is not None:
            return self.terminal

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds

        while True:
            try:
                observed = await self.host._exchange_async(self.message)
            except Exception as cause:
                return CleanupObservation(
                    revision=self.revision,
                    settled=False,
                    allowance_expired=True,
                    failures=(Failure("cleanup", cause),),
                )

            self.revision = observed.revision

            if observed.settled:
                self.terminal = observed

                if self.on_settled is not None:
                    self.on_settled()

                return observed

            if observed.failures or loop.time() >= deadline:
                return observed

            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))


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
        self.release = None

    def _message(self, action, **extra):
        return {"action": action, "analysis": self.analysis.identity, "operation": self.reference.operation_id, **extra}

    def _unavailable(self, cause):
        host = self.analysis.host
        exited = host.process is not None and host.process.poll() is not None

        if self.failure_observation is not None and self.failure_observation.finished == exited:
            return self.failure_observation

        observed = Observation(
            operation=self.reference,
            target=self.invocation.target,
            revision=(self.last_observation.revision + 1 if self.last_observation is not None else 0),
            acceptance=AcceptanceState.OWNED if self.acknowledged else AcceptanceState.UNCERTAIN,
            dispatch=DispatchState.UNKNOWN,
            finished=exited,
            outcome=None,
            result_state=ResultState.NONE,
            result=None,
            states=(),
            progress=(),
            progress_dropped=0,
            failures=(Failure("backend", cause),),
            stop_requested=False,
            allowance_expired=False,
            resources=ResourceState.UNKNOWN,
        )
        self.failure_observation = observed
        return observed

    async def observe(self, *, after, wait_seconds):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds

        while True:
            try:
                observed = await self.analysis.host._exchange_async(self._message("observe"))
            except (RemoteScienceError, LookupError) as cause:
                observed = self._unavailable(cause)

                if not observed.finished:
                    # The channel is gone, not the scientific owner. Give the
                    # workflow its original uncertainty instead of polling forever.
                    raise RemoteScienceError(
                        "Scientific operation may still be running in an unobservable worker"
                    ) from cause

            if observed.operation != self.reference or observed.target != self.invocation.target:
                raise RemoteScienceError("Scientific worker observation identifies different work")

            self.last_observation = observed

            if (after is None or after != observed.revision or observed.finished or
                    loop.time() >= deadline):
                return observed

            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))

    def request_stop(self):
        self.analysis.host._exchange(self._message("stop_operation"))

    async def read_result(self, result: ResultRef) -> bytes:
        return await self.analysis.host._exchange_async(self._message("read_result", result=result))

    def release_result(self, result: ResultRef):
        if self.release is None:
            self.release = _ProcessCleanup(self.analysis.host, self._message("release_result", result=result))
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
        if self.closed:
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

        try:
            outcome = self.host._exchange({"action": "submit", "invocation": invocation})
        except RemoteScienceError:
            # Submission may have entered science. The original identity stays
            # owned and can only be observed, never transmitted again.
            operation = _ProcessOperation(self, invocation, acknowledged=False)
        else:
            if "refusal" in outcome:
                return outcome["refusal"]

            operation = _ProcessOperation(self, invocation, acknowledged=True)

            if outcome["operation"] != operation.reference:
                raise RemoteScienceError("Scientific worker admitted another operation identity")

        self.operations[invocation.operation_id] = operation
        return operation

    def request_stop(self):
        self.closed = True
        self.host._exchange({"action": "stop_analysis", "analysis": self.identity})

    def close(self):
        if self.cleanup is None:
            self.closed = True
            self.cleanup = _ProcessCleanup(self.host, {"action": "close_analysis", "analysis": self.identity})
            self.cleanup.on_settled = lambda: self.host._release_analysis(self)

        return self.cleanup


class ProcessExecutionHost:
    """Own one installed scientific child, with no automatic replay or restart."""

    capabilities = BackendCapabilities(
        contract_versions=frozenset({1}),
        isolation=frozenset({IsolationRequirement.SHARED_PROCESS, IsolationRequirement.DEDICATED_PROCESS}),
        concurrency=frozenset({ConcurrencyRequirement.INSTANCE_SERIAL, ConcurrencyRequirement.PARALLEL}),
        interruption=frozenset({InterruptionSupport.NONE, InterruptionSupport.COOPERATIVE}),
        progress=True,
    )

    def __init__(
        self, *, input_bytes: int, result_bytes: int, state_values: int,
        progress_records: int, initialization_seconds: float, retirement_seconds: float,
    ):
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

    async def __aenter__(self):
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
        self.start_task = asyncio.create_task(self._exchange_async(message, seconds=self.initialization_seconds + 5))
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

    def _exchange(self, message, seconds=2):
        if self.channel is None:
            raise RemoteScienceError("No scientific process is connected")

        return self.channel.exchange(message, seconds)

    async def _exchange_async(self, message, seconds=2):
        return await asyncio.to_thread(self._exchange, message, seconds)

    def open_analysis(self, *, allocations: frozenset[AllocationRef]):
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
        if self.hosted is None or reference != self.hosted.allocation:
            raise ValueError("Process retirement requires the current allocation reference")

        return self.retire()

    def retire(self):
        if self.retirement is None:
            self.retiring = True
            self.retirement = _Cleanup(self.retirement_seconds)
            self.retirement.start(self._shutdown)

        return self.retirement

    async def _shutdown(self):
        failures = []

        if self.process is None:
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
            if self.channel is not None:
                self.channel.close()

            if self.stderr is not None:
                self.stderr.close()

        if failures:
            raise BaseExceptionGroup("Scientific worker retirement left unresolved evidence", failures)
