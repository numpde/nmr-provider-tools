"""Real TLS recovery witness, with a fault inside the current HTTP owner.

The scientific worker is a persistent counting subprocess, not a native provider.
The outer test owns this entire process group and stops it only after the verdict.
This fixture does not set or bypass the installed SDK's launcher policy.
"""

import asyncio
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import time
from unittest.mock import Mock, patch


# Fixed model and cleanup deadlines make the behavioral witness reproducible.
# repo-check: allow-module-constant
MODEL_SECONDS = 0.75
# repo-check: allow-module-constant
CLEANUP_SECONDS = 0.1
# The next Job must complete before the outer fixture begins teardown.
# repo-check: allow-module-constant
RECOVERY_SECONDS = 3.0


def _event(path, kind, **facts):
    document = {"kind": kind, "pid": os.getpid(), "at": time.monotonic(), **facts}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, json.dumps(document).encode() + b"\n")
    finally:
        os.close(descriptor)


def install_fault(stack, path):
    """Hold actual stream cleanup after observing the original read cancellation.

    Patch the same installed HTTP implementation in the baseline parent and in
    the new exec child. A complete real request must precede this fault. The
    marker proves that moving HTTP did not silently remove the injection.
    """
    from provider_core.network import _OwnedStream
    from provider_core.transport import HttpTransport

    active = ContextVar("test_model_cleanup_fault", default=None)
    perform, read, close = HttpTransport._perform, _OwnedStream.read, _OwnedStream.aclose

    async def marked_perform(self, request, *args, **kwargs):
        state = None
        if request.url.path == "/hang":
            state = {"cancelled": False, "engaged": False}
        token = active.set(state)
        try:
            return await perform(self, request, *args, **kwargs)
        finally:
            active.reset(token)

    async def observed_read(self, *args, **kwargs):
        try:
            return await read(self, *args, **kwargs)
        except asyncio.CancelledError:
            state = active.get()
            if state is not None:
                state["cancelled"] = True
                _event(path, "read_cancelled")
            raise

    async def held_close(self):
        state = active.get()
        if state is not None and state["cancelled"]:
            if not state["engaged"]:
                state["engaged"] = True
                _event(path, "cleanup_engaged")
            # A real HTTP implementation that cannot finish its cancellation
            # cleanup. No test-side release occurs before the recovery verdict.
            while True:
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    _event(path, "cleanup_ignored_cancellation")
        await close(self)

    stack.enter_context(patch.object(HttpTransport, "_perform", marked_perform))
    stack.enter_context(patch.object(_OwnedStream, "read", observed_read))
    stack.enter_context(patch.object(_OwnedStream, "aclose", held_close))


def http_worker(path):
    """Test-only exec entrypoint; stdout remains the production reply protocol."""
    from provider_core.chat_worker import main

    def ignore_termination(_number, _frame):
        _event(path, "term_ignored")

    signal.signal(signal.SIGTERM, ignore_termination)
    with ExitStack() as stack:
        install_fault(stack, path)
        main()


def warm_worker():
    """One process initialization, with observable preparation/execution counts."""
    state = {"pid": os.getpid(), "model_loads": 1, "preparations": 0, "executions": 0, "retirements": 0}
    handles = set()
    for line in sys.stdin:
        request = json.loads(line)
        operation, job = request["operation"], request.get("job")
        if operation == "prepare":
            if job in handles:
                raise AssertionError("Fixture preparation was repeated")
            handles.add(job)
            state["preparations"] += 1
        elif operation == "execute":
            if job not in handles:
                raise AssertionError("Fixture execution did not own its prepared handle")
            handles.remove(job)
            state["executions"] += 1
        elif operation == "retire":
            handles.discard(job)
            state["retirements"] += 1
        elif operation != "snapshot":
            raise AssertionError("Unknown fixture worker request")
        print(json.dumps(state), flush=True)


class WarmWorker:
    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(Path(__file__).resolve()), "warm-worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.initial = await self.call("snapshot")

    async def call(self, operation, job=None):
        self.process.stdin.write(json.dumps({"operation": operation, "job": job}).encode() + b"\n")
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), 1)
        if not line:
            raise AssertionError("The persistent worker stopped before the recovery verdict")
        return json.loads(line)


def _failure_types(error):
    from provider_core.failure_projection import owned_relationships

    seen = set()
    types = set()

    def visit(current):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        types.add(type(current).__name__)
        visit(current.__cause__)
        visit(current.__context__)
        for _, child in owned_relationships(current):
            visit(child)
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                visit(child)

    visit(error)
    return sorted(types)


async def scenario(marker, report):
    from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
    from provider_core.api_config import https_trust
    from provider_core.attempt_record import ProtectedEvidence, StartPending
    from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
    from provider_core.controller import AttemptController, LiveStartPolicy
    from provider_core.conversation import ConversationRetirementFailed, ToolFinished
    from provider_core.interpreter import InterpretationFailed, Interpreter
    from provider_core.job_input import verify_job_input
    from provider_core.ownership import AttemptClaims, LiveAttemptLimits
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.attempt_store import record, scope
    from tests.fakes.attempts import observation, progress_response, retained_start, start_response
    from tests.fakes.checkpoint_execution import RecordingDelivery
    from tests.fakes.conversation import call
    from tests.fakes.job_input import response
    from tests.fakes.tls_peer import TlsPeer

    class ApiTransport(Transport):
        def __init__(self, job, index):
            super().__init__()
            self.job = job
            self.reference = "execution_attempt:sha256:" + str(index) * 64

        async def exchange(self, request, *, maximum_bytes, owner=None):
            self.calls.append((request, maximum_bytes))
            path = request.url.path
            if path.endswith("/start"):
                return reply(start_response(job_ref=self.job, execution_attempt_ref=self.reference))
            if path.endswith("/progress"):
                sent = json.loads(request.content)
                return reply(progress_response(execution_attempt_ref=self.reference,
                                               phase=sent["phase"], condition_code=sent["condition_code"]))
            if request.method == "GET":
                return reply(observation(job_ref=self.job, execution_attempt_ref=self.reference))
            raise AssertionError("Unexpected API request in the recovery fixture")

    class Session:
        def __init__(self, job, endpoint, trust, worker):
            self.job, self.worker = job, worker
            self.prepare_calls = self.execute_calls = self.tool_calls = self.retire_calls = 0
            self.handle = None
            self.failure = None
            self.retired = False
            factory = ChatEndpointFactory(
                spec=ChatEndpointSpec(configuration_id="recovery-fixture", model="fixture", url=endpoint,
                                      api_key="PRIVATE_FIXTURE_KEY", reasoning_effort=None),
                trust=trust, retirement_seconds=CLEANUP_SECONDS,
                request_bytes=32768, response_bytes=8192,
            )
            self.interpreter = Interpreter((factory,), timeout_seconds=MODEL_SECONDS,
                                           context_bytes=16384, secrets=("PRIVATE_FIXTURE_KEY",))

        async def prepare(self, acknowledged, instructions, *, report_condition):
            self.prepare_calls += 1
            if acknowledged.start.input != instructions.identity:
                raise AssertionError("Fixture preparation changed its original input")

            async def dispatch(name, arguments, *, not_after):
                if name != "prepare" or arguments:
                    raise AssertionError("Unexpected model tool")
                report_condition.require_execution()
                self.tool_calls += 1
                await self.worker.call("prepare", self.job)
                self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", self.job.encode()))
                return ToolFinished(self.handle)

            tools = [{"type": "function", "function": {
                "name": "prepare", "parameters": {"type": "object", "properties": {}}}}]
            try:
                result = await self.interpreter.run([], tools, dispatch)
            except InterpretationFailed as failure:
                self.failure = failure
                failed = AnalysisFailure(
                    "interpreter_failed", "The model request did not complete within its time allowance.",
                    ProtectedEvidence("fixture.interpreter.failure.v1", b"original failure retained by session"),
                )
            except ConversationRetirementFailed as failure:
                self.failure = failure
                raise
            else:
                return result.value
            return failed

        async def execute(self, prepared, *, report_condition):
            if prepared is not self.handle or self.handle is None:
                raise AssertionError("Fixture execution changed its prepared handle")
            report_condition.require_execution()
            self.execute_calls += 1
            result = await self.worker.call("execute", self.job)
            self.handle = None
            return AnalysisSuccess("fixture.result.v1", json.dumps(result).encode(),
                                   ProtectedEvidence("fixture.result.evidence.v1", b"one fixture execution"))

        async def retire(self):
            self.retire_calls += 1
            await self.interpreter.retire()
            if not self.interpreter.retirement_confirmed:
                raise AssertionError("Original interpreter has not confirmed retirement")
            if not self.retired:
                await self.worker.call("retire", self.job)
                self.retired = True

    worker = WarmWorker()
    await worker.start()
    claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
    stopping = asyncio.Event()
    controllers, sessions, finished, started, paths = [], [], [], [], []
    warm = asyncio.Event()
    timing = {}
    lane_failure = None

    def response_mode(request, body):
        path = request.split(b" ")[1].decode()
        paths.append(path)
        # silent-defaults: allow-contract -- Only the designated hung request stalls; the surrounding Jobs exercise the same healthy endpoint.
        return "headers-stall" if path == "/hang" else "success"

    body = json.dumps({"choices": [{"message": call("prepared", "prepare", {}),
                                  "finish_reason": "tool_calls"}]}).encode()
    with ExitStack() as stack:
        install_fault(stack, marker)
        try:
            import provider_core.chat_process as chat_process
        except ModuleNotFoundError as error:
            # The red base has no process transport. Detection only wires the
            # same fault after migration; acceptance never tests module presence.
            if error.name != "provider_core.chat_process":
                raise
        else:
            command = (sys.executable, "-I", "-B", str(Path(__file__).resolve()), "http-worker", str(marker))
            stack.enter_context(patch.object(chat_process, "_worker_command", return_value=command))

        async with TlsPeer("success", response_body=body, response_mode=response_mode) as peer:
            trust = https_trust((Path(peer.scratch.name) / "cert.pem").read_bytes(), label="B1 fixture")

            async def lane():
                nonlocal lane_failure
                try:
                    for index, name in enumerate(("warmup", "hung", "next"), start=1):
                        job = "job:" + name
                        base = record("recovery:" + name)
                        pending = StartPending(scope(), base.analysis_kind_ref, replace(base.input, job_ref=job),
                                               retained_start(job_ref=job, provider_attempt_key="recovery:" + name))
                        # silent-defaults: allow-contract -- The middle Job alone selects the injected fault; warmup and recovery use the healthy route.
                        session = Session(job, peer.origin + ("/hang" if name == "hung" else "/ready"), trust, worker)
                        transport = ApiTransport(job, index)
                        controller = AttemptController.live(
                            api(transport, scope().api_origin), claims, session,
                            scope=scope(), stopping=stopping,
                            start_policy=LiveStartPolicy(0.01, Mock(), Mock()),
                            preverified_input=verify_job_input(response(job_ref=job), feed=pending.input),
                            delivery=RecordingDelivery(), on_delivery_retry=Mock(),
                            allowed_job_states=frozenset({"open"}),
                            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                            poll_seconds=1, task_settle_seconds=0.1,
                        )
                        controllers.append(controller)
                        sessions.append(session)
                        started.append(name)
                        if name == "hung":
                            timing["started"] = time.monotonic()
                            warm.set()
                        await controller.run(pending)
                        finished.append(name)
                        if name == "next":
                            timing["completed"] = time.monotonic()
                except Exception as error:
                    lane_failure = error
                    warm.set()

            task = asyncio.create_task(lane())
            await asyncio.wait_for(warm.wait(), 4)
            # silent-defaults: allow-contract -- A failure before hung-Job admission still receives a bounded verdict wait; it cannot satisfy the recovery assertions.
            remaining = max(0, timing.get("started", time.monotonic()) + RECOVERY_SECONDS - time.monotonic())
            await asyncio.wait({task}, timeout=remaining)
            final_worker = await worker.call("snapshot")
            # silent-defaults: allow-contract -- An absent fault log supplies zero positive fault evidence and must fail the explicit engagement assertions.
            events = [] if not marker.exists() else [json.loads(line) for line in marker.read_text().splitlines()]
            engaged = [event for event in events if event["kind"] == "cleanup_engaged"]
            # silent-defaults: allow-contract -- Missing engagement records no owner PID; the test independently requires engagement and a distinct child PID.
            fault_pid = engaged[0]["pid"] if engaged else None
            alive = False
            if fault_pid is not None:
                try:
                    os.kill(fault_pid, 0)
                # silent-defaults: allow-contract -- ESRCH establishes that the positively recorded fault owner's PID is no longer live.
                except ProcessLookupError:
                    pass
                else:
                    alive = True
            failures = set(_failure_types(lane_failure))
            for session in sessions:
                failures.update(_failure_types(session.failure))
            result = {
                "provider_pid": os.getpid(), "started_jobs": started, "finished_jobs": finished,
                "next_job_completed": "next" in finished,
                "recovery_budget_seconds": RECOVERY_SECONDS,
                # silent-defaults: allow-contract -- An unfinished next Job has no elapsed completion time and fails its separate completion assertion first.
                "next_job_seconds": None if "completed" not in timing else timing["completed"] - timing["started"],
                "peer_paths": paths, "warm_worker_before": worker.initial, "warm_worker_after": final_worker,
                "session_preparations": [session.prepare_calls for session in sessions],
                "session_executions": [session.execute_calls for session in sessions],
                "tool_calls": sum(session.tool_calls for session in sessions),
                "claim_count": len(claims.live_owners()), "failure_types": sorted(failures),
                "deliveries": [item.operation.value for controller in controllers for item in controller.delivery.items],
                "fault": {"read_cancelled": any(event["kind"] == "read_cancelled" for event in events),
                          "cleanup_engaged": bool(engaged), "pid": fault_pid,
                          "term_ignored": any(event["kind"] == "term_ignored" for event in events),
                          "owner_still_alive": alive},
            }
            # Snapshot the verdict while the provider and warm worker still live.
            # The outer test can now retire the process group without turning
            # teardown into an apparent successful recovery.
            temporary = report.with_suffix(".pending")
            temporary.write_text(json.dumps(result, sort_keys=True))
            temporary.replace(report)
            await asyncio.Event().wait()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if sys.argv[1] == "warm-worker":
        warm_worker()
    elif sys.argv[1] == "http-worker":
        http_worker(Path(sys.argv[2]))
    elif sys.argv[1] == "scenario":
        asyncio.run(scenario(Path(sys.argv[2]), Path(sys.argv[3])))
    else:
        raise SystemExit("Unknown chat recovery fixture role")
