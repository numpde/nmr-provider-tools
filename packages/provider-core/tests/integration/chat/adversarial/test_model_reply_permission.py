"""Live model permission and cross-Attempt retirement retain their original owners."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch

from provider_core.analysis import AnalysisOutcomeUnresolved, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence, StartPending
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.conversation import ToolFinished
from provider_core.execution import ExecutionStopped, ObservationRecovery
from provider_core.failure_projection import owned_relationships
from provider_core.interpreter import Interpreter
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull, OwnershipRefused
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


class InterruptedRefusalSession(InterpretedSession):
    """Keep cleanup pending after refusal until the actual executor interrupts it."""

    def __init__(self, peer):
        super().__init__(peer)
        self.refused = asyncio.Event()
        self.interruption = self.interruption_cause = None

    async def prepare(self, acknowledged, instructions, *, report_condition):
        try:
            return await super().prepare(acknowledged, instructions, report_condition=report_condition)
        except ExecutionStopped:
            self.refused.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as interruption:
                self.interruption = interruption
                if self.interruption_cause is not None:
                    raise interruption from self.interruption_cause
                raise


class ShieldedSession(InterpretedSession):
    """Mirror NMRPeak's shielded interpreter task and once-observed join failure."""

    def __init__(self, peer):
        super().__init__(peer)
        self.original = None
        self.observed = False
        self.outer_cancelled = asyncio.Event()
        self.resume_monitor = None
        self.retirement_calls = 0
        self.retirement_failure = None
        self.retirement_reentered = asyncio.Event()
        self.release_retirement = asyncio.Event()
        self.cleanup_confirmed = False

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.original = asyncio.create_task(super().prepare(
            acknowledged, instructions, report_condition=report_condition,
        ))
        # Register before shield's completion callback: the monitor may resume
        # before outer prepare consumes the inner task's already-ready failure.
        self.original.add_done_callback(lambda task: self.resume_monitor())
        try:
            return await asyncio.shield(self.original)
        except asyncio.CancelledError:
            self.outer_cancelled.set()
            raise
        except BaseException:
            self.observed = True
            raise

    async def retire(self):
        self.retirement_calls += 1
        await super().retire()
        if self.original is not None and not self.observed:
            try:
                await asyncio.shield(self.original)
            except BaseException as failure:
                self.observed = True
                self.retirement_failure = failure
                raise
        self.retirement_reentered.set()
        await self.release_retirement.wait()
        self.cleanup_confirmed = True


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
    def __init__(self, peer, claims, job, session_type):
        self.peer = peer
        base = record("permission:" + job)
        self.pending = StartPending(scope(), base.analysis_kind_ref, replace(base.input, job_ref=job),
                                    retained_start(job_ref=job, provider_attempt_key="permission:" + job))
        self.transport = ObservedAPI(job, "execution_attempt:sha256:" + sha256(job.encode()).hexdigest())
        self.session = session_type(peer)
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
# silent-defaults: allow-contract -- Ordinary fixtures await interpretation directly; explicit variants retain an extra task or await cancellation after refusal.
async def reply_run(claims, job, *, session_type=InterpretedSession):
    release_reply = asyncio.Event()

    async def response_body(request, body):
        await release_reply.wait()
        return json.dumps({"choices": [{"message": call("prepared", "prepare", {}),
                                        "finish_reason": "tool_calls"}]}).encode()

    async with TlsPeer("success", response_factory=response_body) as peer:
        running = ReplyRun(peer, claims, job, session_type)
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
                    try:
                        await asyncio.wait_for(running.controller.settle(), 3)
                    except ControllerFailed:
                        # A failed assertion may leave a deliberately held
                        # claim. Teardown still requires real resource closure.
                        if not running.controller.work_settled:
                            raise


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
            self.assertEqual(1, session.preparations)
            self.assertEqual(1, session.dispatches)
            self.assertLess(session.dispatch_at, session.dispatch_deadline)
            self.assertEqual(1, running.start_count)
            self.assertEqual(1, len(running.peer.requests))
            self.assertEqual(0, session.science.executions)
            self.assertTrue(session.interpreter.retirement_confirmed)
            self.assertTrue(controller.work_settled)
            self.assertEqual([], controller.delivery.items)
            self.assertEqual(0, preparations,
                             msg="A complete timely model reply entered science after the SDK verified " + str(stop))
            if independent_failure is None:
                self.assertIs(await controller.settle_revocation(failed.exception), running.observation.stop.observation)
                self.assertEqual(0, len(claims.live_owners()))
            else:
                self.assertTrue(failure_contains(failed.exception, independent_failure))
                self.assertIsNone(await controller.settle_revocation(failed.exception))
                self.assertEqual(1, len(claims.live_owners()))

        if independent_failure is None:
            await self._next_job(claims)

    async def _next_job(self, claims):
        async with reply_run(claims, "job:next") as (running, release_reply):
            running.session.science.release.set()
            release_reply.set()
            await asyncio.wait_for(running.task, 3)
            self.assertEqual((1, 1, 1), (running.session.preparations, running.session.science.preparations,
                              running.session.science.executions))
            self.assertIs(running.session.science.retired_handle, running.session.science.handle)
            self.assertEqual(1, running.session.science.retirements)
            self.assertEqual(1, running.start_count)
            self.assertEqual(0, len(claims.live_owners()))

    async def test_verified_job_cancellation_prevents_science_before_monitor_resumes(self):
        await self._verified_stop_before_reply({"job_state": "cancelled"}, independent_failure=None)

    async def test_verified_attempt_expiry_prevents_science_before_monitor_resumes(self):
        await self._verified_stop_before_reply({"state": "expired"}, independent_failure=None)

    async def test_independent_failure_after_permission_refusal_keeps_claim(self):
        await self._verified_stop_before_reply({"job_state": "cancelled"},
                                                independent_failure=RuntimeError("independent adapter failure"))

    async def _owned_cancellation_after_refusal(self, independent_failure):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        async with reply_run(claims, "job:refused", session_type=InterruptedRefusalSession) as (running, release_reply):
            session, controller = running.session, running.controller
            session.interruption_cause = independent_failure
            await asyncio.wait_for(running.peer.received.wait(), 3)
            running.transport.stop = {"job_state": "cancelled"}
            await asyncio.wait_for(running.observation.verified.wait(), 3)
            release_reply.set()
            await asyncio.wait_for(session.refused.wait(), 3)
            self.assertEqual(0, session.science.preparations)
            running.observation.resume.set()
            with self.assertRaises(ControllerFailed) as failed:
                await asyncio.wait_for(running.task, 3)
            self.assertIs(failed.exception.cause.cause, running.observation.stop)
            self.assertIsInstance(session.interruption, asyncio.CancelledError)
            self.assertEqual((1, 1, 1), (session.preparations, session.dispatches, running.start_count))
            self.assertEqual(0, session.science.executions)
            self.assertTrue(controller.work_settled)
            self.assertTrue(session.interpreter.retirement_confirmed)
            observed = await asyncio.wait_for(controller.settle_revocation(failed.exception), 3)
            if independent_failure is None:
                self.assertIs(observed, running.observation.stop.observation,
                              msg="A settled SDK-owned cancellation still held the stopped Attempt")
                self.assertEqual(0, len(claims.live_owners()))
            else:
                self.assertIsNone(observed)
                self.assertTrue(failure_contains(failed.exception, independent_failure))
                self.assertEqual(1, len(claims.live_owners()))
            self.assertIs(failed.exception.cause.operation_interruption, session.interruption)
        if independent_failure is None:
            await self._next_job(claims)

    async def test_owned_cancellation_after_permission_refusal_releases_settled_claim(self):
        await self._owned_cancellation_after_refusal(None)

    async def test_owned_cancellation_with_independent_cause_after_refusal_keeps_claim(self):
        await self._owned_cancellation_after_refusal(RuntimeError("independent cancellation cleanup failure"))

    async def test_shielded_interpreter_stop_requires_positive_session_retirement(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        async with reply_run(claims, "job:shielded", session_type=ShieldedSession) as (running, release_reply):
            session, controller = running.session, running.controller
            session.resume_monitor = running.observation.resume.set
            settlement = reentered = None
            try:
                await asyncio.wait_for(running.peer.received.wait(), 3)
                running.transport.stop = {"job_state": "cancelled"}
                await asyncio.wait_for(running.observation.verified.wait(), 3)
                release_reply.set()
                with self.assertRaises(ControllerFailed) as failed:
                    await asyncio.wait_for(running.task, 3)
                self.assertTrue(session.outer_cancelled.is_set())
                self.assertIs(session.original.exception(), running.observation.stop)
                self.assertIs(session.retirement_failure, running.observation.stop)
                self.assertIs(failed.exception.cause.cause, running.observation.stop)
                self.assertEqual(1, session.retirement_calls)
                self.assertFalse(session.cleanup_confirmed)
                self.assertFalse(controller.work_settled)
                self.assertEqual(1, len(claims.live_owners()))
                # Observing the same exception again is not cleanup success.
                # Only the original session's later positive return can release.
                settlement = asyncio.create_task(controller.settle_revocation(failed.exception))
                reentered = asyncio.create_task(session.retirement_reentered.wait())
                done, _ = await asyncio.wait({settlement, reentered}, timeout=3,
                                            return_when=asyncio.FIRST_COMPLETED)
                self.assertIn(reentered, done, msg="Revocation settlement gave up before reobserving its original session")
                self.assertFalse(settlement.done())
                self.assertFalse(session.cleanup_confirmed)
                self.assertEqual(1, len(claims.live_owners()))
                session.release_retirement.set()
                observed = await asyncio.wait_for(settlement, 3)
                self.assertIs(observed, running.observation.stop.observation)
                self.assertTrue(session.cleanup_confirmed)
                self.assertTrue(controller.work_settled)
                self.assertEqual(2, session.retirement_calls)
                self.assertEqual((1, 1, 1), (session.preparations, session.dispatches, running.start_count))
                self.assertEqual((0, 0), (session.science.preparations, session.science.executions))
                self.assertEqual(1, len(running.peer.requests))
                self.assertEqual(0, len(claims.live_owners()))
            finally:
                session.release_retirement.set()
                waits = [task for task in (settlement, reentered) if task is not None]
                for task in waits:
                    if not task.done():
                        task.cancel()
                if waits:
                    await asyncio.gather(*waits, return_exceptions=True)
        await self._next_job(claims)

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
            self.assertEqual(1, len(claims.live_owners()))
            self.assertFalse(original.done())
            self.assertEqual(1, science.preparations)
            science.release.set()
            observed = await asyncio.wait_for(controller.settle_revocation(failed.exception), 3)
            self.assertIs(observed, running.observation.stop.observation)
            self.assertIs(science.pending, original)
            self.assertIs(original.result(), science.handle)
            self.assertIs(science.retired_handle, science.handle)
            self.assertEqual((1, 1, 0, 1, 1), (running.session.preparations, science.preparations, science.executions,
                              science.retirements, running.start_count))
            self.assertTrue(running.session.interpreter.retirement_confirmed)
            self.assertEqual(0, len(claims.live_owners()))
        await self._next_job(claims)


class HeldRetirementSession(InterpretedSession):
    """Keep the original session's retirement pending after its model child exits."""

    def __init__(self, peer):
        super().__init__(peer)
        self.retiring = asyncio.Event()
        self.release_retirement = asyncio.Event()
        self.retirement_completed = False

    async def retire(self):
        await super().retire()
        self.retiring.set()
        await self.release_retirement.wait()
        self.retirement_completed = True


class UnresolvedOutcomeSession(InterpretedSession):
    """Science entered exactly once, but its result was never established."""

    async def execute(self, prepared, *, report_condition):
        await super().execute(prepared, report_condition=report_condition)
        raise AnalysisOutcomeUnresolved("The original scientific outcome is unconfirmed")


class ComposedLifecycleOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Two real controllers share claims, not interpreter children or science."""

    async def test_held_original_retirement_does_not_block_independent_attempt(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(
            maximum_attempts=2, maximum_control_bytes=2 * 131072,
            maximum_pending_terminal_bytes=2 * 2097152,
        ))
        release_reply = asyncio.Event()

        async def held_reply(_request, _body):
            await release_reply.wait()
            return json.dumps({"choices": [{"message": call("held", "prepare", {}),
                                            "finish_reason": "tool_calls"}]}).encode()

        successful_reply = json.dumps({"choices": [{"message": call("ready", "prepare", {}),
                                                     "finish_reason": "tool_calls"}]}).encode()
        async with (TlsPeer("success", response_factory=held_reply) as stalled_peer,
                    TlsPeer("success", response_body=successful_reply) as healthy_peer):
            held = ReplyRun(stalled_peer, claims, "job:held", HeldRetirementSession)
            healthy = ReplyRun(healthy_peer, claims, "job:healthy", InterpretedSession)
            original = independent = None
            try:
                # The scoped observer exposes the original server stop after the
                # controller has processed it, before the model reply is released.
                # No other controller is running while asyncio.wait is patched.
                with patch("provider_core.execution.asyncio.wait", new=held.observation):
                    original = asyncio.create_task(held.controller.run(held.pending))
                    await asyncio.wait_for(stalled_peer.received.wait(), 5)
                    held.transport.stop = {"job_state": "cancelled"}
                    await asyncio.wait_for(held.observation.verified.wait(), 5)
                    release_reply.set()
                    await asyncio.wait_for(held.session.dispatch_finished.wait(), 5)
                    held.observation.resume.set()
                    with self.assertRaises(ControllerFailed) as caught:
                        await asyncio.wait_for(asyncio.shield(original), 5)

                failure = caught.exception
                self.assertIs(failure.cause.cause, held.observation.stop)
                await asyncio.wait_for(held.session.retiring.wait(), 5)
                self.assertTrue(held.session.interpreter.retirement_confirmed)
                self.assertFalse(held.session.retirement_completed)
                self.assertFalse(held.controller.work_settled)
                self.assertEqual((held.controller,), claims.live_owners())
                self.assertEqual((1, 1, 0, 0), (
                    held.start_count, held.session.dispatches,
                    held.session.science.preparations, held.session.science.executions,
                ))
                with self.assertRaises(OwnershipRefused):
                    claims.claim_live(held.pending, lifecycle_owner=object())

                # B performs real model HTTP, preparation, execution, handoff,
                # session retirement and claim release while A remains held.
                healthy.session.science.release.set()
                independent = asyncio.create_task(healthy.controller.run(healthy.pending))
                result = await asyncio.wait_for(asyncio.shield(independent), 6)
                self.assertIs(type(result), DeliveryAcceptedCheckpoint)
                self.assertTrue(healthy.controller.shutdown_ready)
                self.assertEqual((1, 1, 1, 1), (
                    healthy.start_count, healthy.session.dispatches,
                    healthy.session.science.preparations, healthy.session.science.executions,
                ))
                self.assertEqual(1, len(healthy.controller.delivery.items))
                self.assertEqual(1, len(healthy_peer.requests))
                self.assertEqual(1, len(stalled_peer.requests))
                self.assertEqual((held.controller,), claims.live_owners(),
                                 "The independent terminal handoff released A's claim")
                self.assertFalse(held.session.retirement_completed)

                # Only positive retirement of A's original session and the
                # original verified revocation can now release its identity.
                held.session.release_retirement.set()
                observed = await asyncio.wait_for(
                    held.controller.settle_revocation(failure), 6)
                self.assertIs(observed, held.observation.stop.observation)
                self.assertTrue(held.session.retirement_completed)
                self.assertTrue(held.controller.shutdown_ready)
                self.assertEqual((), claims.live_owners())
                self.assertEqual([], held.controller.delivery.items)
                self.assertEqual(0, held.session.science.executions)
            finally:
                release_reply.set()
                held.observation.resume.set()
                held.session.release_retirement.set()
                held.session.science.release.set()
                for controller, task in ((held.controller, original),
                                         (healthy.controller, independent)):
                    if task is not None and not task.done():
                        controller.request_stop()
                tasks = tuple(task for task in (original, independent) if task is not None)
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 8)

    async def test_retired_science_with_unknown_outcome_remains_claimed(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        reply_body = json.dumps({"choices": [{"message": call("unknown", "prepare", {}),
                                               "finish_reason": "tool_calls"}]}).encode()
        async with TlsPeer("success", response_body=reply_body) as peer:
            original = ReplyRun(peer, claims, "job:unknown", UnresolvedOutcomeSession)
            original.session.science.release.set()
            task = asyncio.create_task(original.controller.run(original.pending))
            try:
                with self.assertRaises(ControllerFailed) as caught:
                    await asyncio.wait_for(asyncio.shield(task), 6)
                self.assertIsInstance(caught.exception.cause.cause, AnalysisOutcomeUnresolved)
                self.assertTrue(original.controller.work_settled)
                self.assertTrue(original.controller.shutdown_ready)
                self.assertTrue(original.session.interpreter.retirement_confirmed)
                self.assertIs(original.session.science.retired_handle,
                              original.session.science.handle)
                self.assertEqual((1, 1, 1), (
                    original.start_count, original.session.science.preparations,
                    original.session.science.executions,
                ))
                self.assertEqual([], original.controller.delivery.items)
                self.assertEqual((original.controller,), claims.live_owners())
                self.assertIsNone(await original.controller.settle_revocation(caught.exception))
                await original.controller.settle()
                self.assertEqual((original.controller,), claims.live_owners())
                successor = ReplyRun(peer, claims, "job:successor", InterpretedSession)
                with self.assertRaises(ControllerFailed) as refused:
                    await asyncio.wait_for(
                        successor.controller.run(successor.pending), 3)
                self.assertIs(type(refused.exception.cause), LiveCapacityFull)
                self.assertTrue(successor.controller.shutdown_ready)
                self.assertEqual((0, 0), (successor.start_count, successor.session.preparations))
                self.assertEqual((original.controller,), claims.live_owners())
                self.assertEqual(1, len(peer.requests),
                                 "Capacity refusal dispatched another model request")
            finally:
                if not task.done():
                    original.controller.request_stop()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 8)
