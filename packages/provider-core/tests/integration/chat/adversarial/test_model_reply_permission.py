"""A complete HTTP reply cannot restore an Attempt's verified withdrawn permission."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence, StartPending
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.conversation import ToolFinished
from provider_core.execution import ExecutionStopped, ObservationRecovery
from provider_core.failure_projection import owned_relationships
from provider_core.interpreter import Interpreter
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, retained_start, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.conversation import call
from tests.fakes.job_input import response
from tests.fakes.tls_peer import TlsPeer


class ObservedAPI(Transport):
    """Supply correlated API bytes through the real SDK decoder and executor."""

    def __init__(self, job, reference):
        super().__init__()
        self.job, self.reference = job, reference
        self.stop = {}

    async def exchange(self, request, *, maximum_bytes, owner=None):
        self.calls.append((request, maximum_bytes))
        if request.url.path.endswith("/start"):
            return reply(start_response(job_ref=self.job, execution_attempt_ref=self.reference))
        if request.url.path.endswith("/progress"):
            sent = json.loads(request.content)
            return reply(progress_response(execution_attempt_ref=self.reference,
                                           phase=sent["phase"], condition_code=sent["condition_code"]))
        if request.method == "GET":
            return reply(observation(job_ref=self.job, execution_attempt_ref=self.reference, **self.stop))
        raise AssertionError("Unexpected API operation in the reply-permission fixture")


class CountingScience:
    """Count actual preparation entry and retain its original asynchronous owner."""

    def __init__(self):
        self.preparations = self.executions = self.retirements = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pending = None
        self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", b"original candidate"))
        self.retired_handle = None

    async def prepare(self):
        if self.pending is not None:
            raise AssertionError("Scientific preparation was repeated")
        self.preparations += 1
        self.pending = asyncio.create_task(self._prepare())
        self.entered.set()
        return await asyncio.shield(self.pending)

    async def _prepare(self):
        await self.release.wait()
        return self.handle

    async def execute(self, prepared):
        if prepared is not self.handle or self.executions:
            raise AssertionError("Scientific execution replaced or repeated its prepared handle")
        self.executions += 1
        return AnalysisSuccess("fixture.result.v1", b"one execution", ProtectedEvidence("fixture.result.v1", b"evidence"))

    async def retire(self):
        if self.pending is not None and self.retired_handle is None:
            self.retired_handle = await asyncio.shield(self.pending)
            self.retirements += 1


class InterpretedSession:
    def __init__(self, peer):
        self.science = CountingScience()
        self.preparations = self.dispatches = 0
        self.dispatch_finished = asyncio.Event()
        self.dispatch_at = self.dispatch_deadline = None
        self.after_refusal = None
        factory = ChatEndpointFactory(
            spec=ChatEndpointSpec(configuration_id="permission-fixture", model="fixture", url=peer.origin + "/chat",
                                  api_key="PRIVATE_FIXTURE_KEY", reasoning_effort=None),
            trust=peer.trust, retirement_seconds=0.5, request_bytes=32768, response_bytes=8192,
        )
        self.interpreter = Interpreter((factory,), timeout_seconds=10, context_bytes=16384,
                                       secrets=("PRIVATE_FIXTURE_KEY",))

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.preparations += 1
        if acknowledged.start.input != instructions.identity:
            raise AssertionError("Preparation substituted its selected Job input")

        async def dispatch(name, arguments, *, not_after):
            if name != "prepare" or arguments:
                raise AssertionError("Unexpected model tool")
            self.dispatches += 1
            self.dispatch_at, self.dispatch_deadline = asyncio.get_running_loop().time(), not_after
            try:
                try:
                    report_condition.require_execution()
                except ExecutionStopped:
                    if self.after_refusal is not None:
                        raise self.after_refusal
                    raise
                return ToolFinished(await self.science.prepare())
            finally:
                self.dispatch_finished.set()

        tools = [{"type": "function", "function": {
            "name": "prepare", "parameters": {"type": "object", "properties": {}}}}]
        interpreted = await self.interpreter.run([], tools, dispatch)
        return interpreted.value

    async def execute(self, prepared, *, report_condition):
        report_condition.require_execution()
        return await self.science.execute(prepared)

    async def retire(self):
        await self.interpreter.retire()
        await self.science.retire()


class ObservationGap:
    """Extend the monitor's existing wait after its original read verifies stop."""

    def __init__(self, controller):
        self.controller = controller
        self.verified = asyncio.Event()
        self.resume = asyncio.Event()
        self.stop = None
        self.wait = asyncio.wait

    async def __call__(self, tasks, *args, **kwargs):
        done, pending = await self.wait(tasks, *args, **kwargs)
        task = getattr(self.controller.operation, "_observation_wait", None)
        if (not self.verified.is_set() and task in done and not task.cancelled()
                and isinstance(task.exception(), ExecutionStopped)):
            self.stop = task.exception()
            self.verified.set()
            await self.resume.wait()
        return done, pending


class ReplyRun:
    def __init__(self, peer, claims, job):
        self.peer = peer
        base = record("permission:" + job)
        self.pending = StartPending(scope(), base.analysis_kind_ref, replace(base.input, job_ref=job),
                                    retained_start(job_ref=job, provider_attempt_key="permission:" + job))
        self.transport = ObservedAPI(job, "execution_attempt:sha256:" + sha256(job.encode()).hexdigest())
        self.session = InterpretedSession(peer)
        self.controller = AttemptController.live(
            api(self.transport, scope().api_origin), claims, self.session, scope=scope(), stopping=asyncio.Event(),
            start_policy=LiveStartPolicy(1, Mock(), Mock()),
            preverified_input=verify_job_input(response(job_ref=job), feed=self.pending.input),
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=0.01, task_settle_seconds=0.1,
            observation_recovery=ObservationRecovery(10, 0.1, Mock(), Mock()),
        )
        self.observation = ObservationGap(self.controller)
        self.task = None

    @property
    def start_count(self):
        return sum(request.url.path.endswith("/start") for request, _ in self.transport.calls)


@asynccontextmanager
async def reply_run(claims, job):
    release_reply = asyncio.Event()

    async def response_body(request, body):
        await release_reply.wait()
        return json.dumps({"choices": [{"message": call("prepared", "prepare", {}),
                                        "finish_reason": "tool_calls"}]}).encode()

    async with TlsPeer("success", response_factory=response_body) as peer:
        running = ReplyRun(peer, claims, job)
        with patch("provider_core.execution.asyncio.wait", new=running.observation):
            running.task = asyncio.create_task(running.controller.run(running.pending))
            try:
                yield running, release_reply
            finally:
                release_reply.set()
                running.observation.resume.set()
                running.session.science.release.set()
                if not running.task.done():
                    running.controller.request_stop()
                await asyncio.wait_for(asyncio.gather(running.task, return_exceptions=True), 3)
                if not running.controller.work_settled:
                    await asyncio.wait_for(running.controller.settle(), 3)


def failure_contains(root, expected):
    seen = set()

    def visit(error):
        if error is None or id(error) in seen:
            return False
        seen.add(id(error))
        if error is expected:
            return True
        return (visit(error.__cause__) or visit(error.__context__)
                or any(visit(child) for _, child in owned_relationships(error)))

    return visit(root)


class ModelReplyPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def _verified_stop_before_reply(self, stop, *, independent_failure):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        async with reply_run(claims, "job:stopped") as (running, release_reply):
            session, controller = running.session, running.controller
            session.science.release.set()
            session.after_refusal = independent_failure
            await asyncio.wait_for(running.peer.received.wait(), 3)
            running.transport.stop = stop
            await asyncio.wait_for(running.observation.verified.wait(), 3)
            # The real correlated read has finished, but its original monitor
            # remains inside its existing wait. No fake yield splits a gate
            # from the scientific submission it protects.
            self.assertFalse(running.observation.resume.is_set())
            release_reply.set()
            await asyncio.wait_for(session.dispatch_finished.wait(), 3)
            preparations = session.science.preparations
            running.observation.resume.set()
            with self.assertRaises(ControllerFailed) as failed:
                await asyncio.wait_for(running.task, 3)
            self.assertIs(failed.exception.cause.cause, running.observation.stop)
            self.assertEqual(session.preparations, 1)
            self.assertEqual(session.dispatches, 1)
            self.assertLess(session.dispatch_at, session.dispatch_deadline)
            self.assertEqual(running.start_count, 1)
            self.assertEqual(len(running.peer.requests), 1)
            self.assertEqual(session.science.executions, 0)
            self.assertTrue(session.interpreter.retirement_confirmed)
            self.assertTrue(controller.work_settled)
            self.assertEqual(controller.delivery.items, [])
            self.assertEqual(preparations, 0,
                             msg="A complete timely model reply entered science after the SDK verified " + str(stop))
            if independent_failure is None:
                self.assertIs(await controller.settle_revocation(failed.exception), running.observation.stop.observation)
                self.assertEqual(len(claims.live_owners()), 0)
            else:
                self.assertTrue(failure_contains(failed.exception, independent_failure))
                self.assertIsNone(await controller.settle_revocation(failed.exception))
                self.assertEqual(len(claims.live_owners()), 1)

        if independent_failure is None:
            await self._next_job(claims)

    async def _next_job(self, claims):
        async with reply_run(claims, "job:next") as (running, release_reply):
            running.session.science.release.set()
            release_reply.set()
            await asyncio.wait_for(running.task, 3)
            self.assertEqual((running.session.preparations, running.session.science.preparations,
                              running.session.science.executions), (1, 1, 1))
            self.assertIs(running.session.science.retired_handle, running.session.science.handle)
            self.assertEqual(running.session.science.retirements, 1)
            self.assertEqual(running.start_count, 1)
            self.assertEqual(len(claims.live_owners()), 0)

    async def test_verified_job_cancellation_prevents_science_before_monitor_resumes(self):
        await self._verified_stop_before_reply({"job_state": "cancelled"}, independent_failure=None)

    async def test_verified_attempt_expiry_prevents_science_before_monitor_resumes(self):
        await self._verified_stop_before_reply({"state": "expired"}, independent_failure=None)

    async def test_independent_failure_after_permission_refusal_keeps_claim(self):
        await self._verified_stop_before_reply({"job_state": "cancelled"},
                                                independent_failure=RuntimeError("independent adapter failure"))

    async def test_timely_reply_starts_science_once(self):
        await self._next_job(AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1)))

    async def test_preparation_admitted_before_stop_keeps_original_owner_until_retirement(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        async with reply_run(claims, "job:admitted") as (running, release_reply):
            science, controller = running.session.science, running.controller
            release_reply.set()
            await asyncio.wait_for(science.entered.wait(), 3)
            original = science.pending
            running.transport.stop = {"job_state": "cancelled"}
            await asyncio.wait_for(running.observation.verified.wait(), 3)
            running.observation.resume.set()
            with self.assertRaises(ControllerFailed) as failed:
                await asyncio.wait_for(running.task, 3)
            self.assertEqual(len(claims.live_owners()), 1)
            self.assertFalse(original.done())
            self.assertEqual(science.preparations, 1)
            science.release.set()
            observed = await asyncio.wait_for(controller.settle_revocation(failed.exception), 3)
            self.assertIs(observed, running.observation.stop.observation)
            self.assertIs(science.pending, original)
            self.assertIs(original.result(), science.handle)
            self.assertIs(science.retired_handle, science.handle)
            self.assertEqual((running.session.preparations, science.preparations, science.executions,
                              science.retirements, running.start_count), (1, 1, 0, 1, 1))
            self.assertTrue(running.session.interpreter.retirement_confirmed)
            self.assertEqual(len(claims.live_owners()), 0)
        await self._next_job(claims)
