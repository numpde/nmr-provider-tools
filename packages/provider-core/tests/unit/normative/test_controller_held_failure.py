"""Exact held lifecycle authority with recording scientific and delivery ports."""

import asyncio
from dataclasses import replace
from itertools import chain, repeat
import unittest
from unittest.mock import Mock, patch
import ssl
import time

from provider_core.analysis import AnalysisOutcomeUnresolved, AnalysisSuccess
from provider_core.api import ApiFailure
from provider_core.attempt_record import StartPending
from provider_core.controller import ControllerFailed, ControllerRefused
from provider_core.diagnostics import failure_evidence
from provider_core.execution import ObservationGapExpired, ObservationRecovery
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull, OwnershipRefused
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadLimits, UploadRecovery, UploadRecoveryExhausted
from tests.fakes.api import Transport, api, io_failure, reply
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import observation, progress_response, retained_start, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.execution import LiveAttemptProbe, PausedSession, Session, live_controller
from tests.fakes.job_input import facts, response
from tests.unit.normative.test_upload_recovery import refusal


class FailedScience(Session):
    def __init__(self, probe, failure):
        super().__init__(probe)
        self.failure = failure
        self.original = OSError(104, "PRIVATE_NATIVE_RESPONSE")

    async def execute(self, prepared, *, report_condition):
        await super().execute(prepared, report_condition=report_condition)
        raise self.failure from self.original


class FailedSource(FailedScience):
    async def prepare(self, acknowledged, instructions, *, report_condition):
        await Session.prepare(self, acknowledged, instructions, report_condition=report_condition)
        raise self.failure from self.original


def pending(suffix):
    job = f"job:held-{suffix}"
    return StartPending(scope(), "nmr_predict", replace(facts(), job_ref=job),
                        retained_start(job_ref=job, provider_attempt_key=f"held:{suffix}"))


def wire_replies(selected, reference):
    observed = reply(observation(job_ref=selected.input.job_ref, execution_attempt_ref=reference))
    return (
        reply(start_response(job_ref=selected.input.job_ref, execution_attempt_ref=reference)),
        observed, reply(response(job_ref=selected.input.job_ref)),
        reply(progress_response(execution_attempt_ref=reference)), observed,
        reply(progress_response(phase="running", execution_attempt_ref=reference)),
        observed, observed,
    )


# silent-defaults: allow-contract -- This recording fixture fails during execute unless a source witness explicitly selects prepare; it admits no operator configuration.
def configured(claims, selected, reference, failure, *, source=False, transport=None, recovery=None):
    probe = LiveAttemptProbe()
    session = (FailedSource if source else FailedScience)(probe, failure)
    delivery = RecordingDelivery()
    transport = transport if transport is not None else Transport(*wire_replies(selected, reference))
    controller = live_controller(
        api(transport, scope().api_origin), probe, claims, session,
        delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.001,
        delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}),
        poll_seconds=0.1, task_settle_seconds=0.1, observation_recovery=recovery,
    )
    return controller, session, delivery, transport


class HeldFailureTests(unittest.IsolatedAsyncioTestCase):
    async def failed_run(self, controller, selected):
        with self.assertRaises(ControllerFailed) as caught:
            await controller.run(selected)
        return caught.exception

    def assert_original_raised(self, controller, selected, failure):
        with self.assertRaises(ControllerFailed) as caught:
            controller.require_held_failure(selected, failure)
        self.assertIs(caught.exception, failure)

    async def test_two_actual_controllers_retain_jobs_attempts_and_capacity_without_fail(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(
            maximum_attempts=2, maximum_control_bytes=262144, maximum_pending_terminal_bytes=4194304,
        ))
        runs = []
        for suffix in ("a", "b"):
            selected = pending(suffix)
            reference = "execution_attempt:sha256:" + suffix * 64
            unresolved = AnalysisOutcomeUnresolved("PRIVATE_BINDING_DETAIL")
            controller, session, delivery, transport = configured(claims, selected, reference, unresolved)
            failure = await self.failed_run(controller, selected)
            self.assertIs(controller.require_held_failure(selected, failure), failure)
            self.assertIs(failure.cause.cause, unresolved)
            self.assertIs(unresolved.__cause__, session.original)
            self.assertEqual(failure.cause.attempt.admission.execution_attempt_ref, reference)
            self.assertTrue(claims.owns_job(selected.scope, selected.input.job_ref))
            self.assertTrue(controller.shutdown_ready)
            controller._claim.require(selected.scope, reference)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertEqual(delivery.items, [])
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(selected, lifecycle_owner=LiveAttemptProbe())
            rendered = repr(failure_evidence(failure, secrets=()))
            self.assertIn(type(session.original).__name__, rendered)
            self.assertIn("104", rendered)
            self.assertIn("retain this Attempt", rendered)
            self.assertNotIn("PRIVATE_NATIVE_RESPONSE", rendered)
            self.assertNotIn("PRIVATE_BINDING_DETAIL", rendered)
            runs.append((controller, selected, failure, transport))
        self.assertEqual(set(claims.live_owners()), {run[0] for run in runs})
        with self.assertRaises(LiveCapacityFull):
            claims.claim_live(pending("c"), lifecycle_owner=LiveAttemptProbe())
        first, second = runs
        for owner, selected, foreign in ((first[0], first[1], second[2]),
                                         (first[0], second[1], first[2])):
            with self.assertRaises(ControllerRefused):
                owner.require_held_failure(selected, foreign)
        for controller, selected, failure, transport in runs:
            calls = len(transport.calls)
            self.assertIs(controller.require_held_failure(selected, failure), failure)
            self.assertEqual(len(transport.calls), calls)
            self.assertTrue(claims.owns_job(selected.scope, selected.input.job_ref))

    async def test_foreign_admission_and_stale_wrapper_cannot_borrow_registered_failure(self):
        claims = AttemptClaims.live()
        selected = pending("a")
        controller, _, _, _ = configured(claims, selected, "execution_attempt:sha256:" + "a" * 64,
                                          AnalysisOutcomeUnresolved("No outcome"))
        failure = await self.failed_run(controller, selected)
        copied = ControllerFailed(failure.cause, ())
        with self.assertRaises(ControllerRefused):
            controller.require_held_failure(selected, copied)
        original = failure.cause.attempt
        failure.cause.attempt = replace(original, admission=replace(
            original.admission, execution_attempt_ref="execution_attempt:sha256:" + "b" * 64,
        ))
        try:
            with self.assertRaises(ControllerRefused):
                controller.require_held_failure(selected, failure)
        finally:
            failure.cause.attempt = original
        self.assertIs(controller.require_held_failure(selected, failure), failure)

    async def test_same_job_with_changed_key_input_or_scope_is_not_the_selected_start(self):
        selected = pending("a")
        controller, _, _, transport = configured(AttemptClaims.live(), selected,
            "execution_attempt:sha256:" + "a" * 64, AnalysisOutcomeUnresolved("No outcome"))
        failure = await self.failed_run(controller, selected)
        calls = len(transport.calls)
        variants = (
            replace(selected, body=retained_start(job_ref=selected.input.job_ref, provider_attempt_key="changed:key")),
            replace(selected, input=replace(selected.input, input_fingerprint="sha256:" + "f" * 64)),
            replace(selected, scope=replace(selected.scope, api_origin="https://other.example")),
        )
        for variant in variants:
            with self.subTest(selected=variant), self.assertRaises(ControllerRefused):
                controller.require_held_failure(variant, failure)
        self.assertEqual(len(transport.calls), calls)
        self.assertIs(controller.require_held_failure(selected, failure), failure)

    async def test_foreign_loop_refusal_keeps_both_original_and_ownership_failures(self):
        selected = pending("a")
        controller, _, _, _ = configured(AttemptClaims.live(), selected,
            "execution_attempt:sha256:" + "a" * 64, AnalysisOutcomeUnresolved("PRIVATE_BINDING_DETAIL"))
        failure = await self.failed_run(controller, selected)

        async def query_from_foreign_loop():
            with self.assertRaises(ControllerRefused) as caught:
                controller.require_held_failure(selected, failure)
            return caught.exception

        refused = await asyncio.to_thread(asyncio.run, query_from_foreign_loop())
        self.assertIs(type(refused.__cause__), ExceptionGroup)
        self.assertIs(refused.__cause__.exceptions[0], failure)
        self.assertIs(type(refused.__cause__.exceptions[1]), OwnershipRefused)
        self.assertNotIn("PRIVATE_BINDING_DETAIL", repr(failure_evidence(refused, secrets=())))
        self.assertIs(controller.require_held_failure(selected, failure), failure)

    async def test_historical_cleanup_failure_still_blocks_after_physical_readiness_recovers(self):
        selected = pending("a")
        controller, session, _, _ = configured(AttemptClaims.live(), selected,
            "execution_attempt:sha256:" + "a" * 64, AnalysisOutcomeUnresolved("No outcome"))
        cleanup = RuntimeError("PRIVATE_CLEANUP")
        session.retirement_failure = cleanup
        failure = await self.failed_run(controller, selected)
        self.assert_original_raised(controller, selected, failure)
        session.retirement_failure = None
        await controller.operation.settle()
        self.assertTrue(controller.shutdown_ready)
        self.assertIn(cleanup, failure.cause.cleanup_failures)
        self.assert_original_raised(controller, selected, failure)

    async def test_source_exhaustion_retains_nested_cleanup_and_interruption_evidence(self):
        cases = (
            ("clean", (), None, True),
            ("cleanup", (RuntimeError("source retirement"),), None, False),
            ("interruption", (), asyncio.CancelledError("source cancellation"), False),
        )
        for condition, cleanup, interruption, admitted in cases:
            with self.subTest(condition=condition):
                outage = ApiFailure("read Upload", "dependency unavailable")
                source = UploadRecoveryExhausted("request allowance exhausted", cause=outage,
                    cleanup_failures=cleanup, interruption=interruption)
                selected = pending("a")
                controller, _, delivery, _ = configured(AttemptClaims.live(), selected,
                    "execution_attempt:sha256:" + "a" * 64, source, source=True)
                failure = await self.failed_run(controller, selected)
                if admitted:
                    self.assertIs(controller.require_held_failure(selected, failure), failure)
                else:
                    self.assert_original_raised(controller, selected, failure)
                self.assertEqual(delivery.items, [])

    async def test_actual_source_acquisition_exhaustion_retains_its_job_without_science(self):
        selected = pending("a")
        reference = "execution_attempt:sha256:" + "a" * 64
        source_transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.1)
        source_api = api(source_transport, scope().api_origin)
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.1)
        source = JobUploads(
            selected.input.job_ref, api=source_api, store=store, store_origin="https://store.example",
            limits=UploadLimits(8, 32, 3, 2),
            recovery=UploadRecovery(3, 0.001, 0.001, asyncio.Event(), Mock()),
        )
        probe = LiveAttemptProbe()

        class AcquiringSession(Session):
            async def prepare(self, acknowledged, instructions, *, report_condition):
                await super().prepare(acknowledged, instructions, report_condition=report_condition)
                await source.inspect()
                raise AssertionError("Unavailable sources must not admit science")

            async def retire(self):
                await super().retire()
                await source_api.retire()
                await store.retire()

        session, delivery = AcquiringSession(probe), RecordingDelivery()
        claims = AttemptClaims.live()
        controller = live_controller(
            api(Transport(*wire_replies(selected, reference)), scope().api_origin), probe, claims, session,
            delivery=delivery, on_delivery_retry=Mock(),
            delivery_retry_initial_seconds=0.001, delivery_retry_max_seconds=0.002,
            allowed_job_states=frozenset({"open"}), poll_seconds=0.1, task_settle_seconds=0.1,
        )
        clients = [refusal(503, "service_unavailable") for _ in range(3)]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            failure = await self.failed_run(controller, selected)
        self.assertIs(type(failure.cause.cause), UploadRecoveryExhausted)
        self.assertEqual(failure.cause.cause.cause.status, 503)
        self.assertIsNone(failure.cause.api_failure)
        self.assertEqual(source.requests_started, 3)
        self.assertTrue(all(client.closed for client in clients))
        self.assertIs(controller.require_held_failure(selected, failure), failure)
        self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
        self.assertEqual(delivery.items, [])
        self.assertTrue(claims.owns_job(selected.scope, selected.input.job_ref))

    async def test_observation_gap_admits_only_its_own_retired_read_outage(self):
        selected = pending("a")
        reference = "execution_attempt:sha256:" + "a" * 64
        transport = Transport()
        transport.replies = chain(
            (reply(start_response(job_ref=selected.input.job_ref, execution_attempt_ref=reference)),),
            repeat(reply(b"PRIVATE_OUTAGE_BODY", status=503)),
        )
        recovery = ObservationRecovery(0.1, 0.01, Mock(), Mock())
        controller, session, delivery, _ = configured(AttemptClaims.live(), selected, reference,
            AnalysisOutcomeUnresolved("must not execute"), transport=transport, recovery=recovery)
        failure = await self.failed_run(controller, selected)
        gap = failure.cause.cause
        self.assertIs(type(gap), ObservationGapExpired)
        self.assertIs(gap._executor, controller.operation)
        self.assertIs(failure.cause.api_failure, gap.cause)
        self.assertIsNotNone(gap.cause)
        self.assertIs(controller.require_held_failure(selected, failure), failure)
        self.assertEqual([event[0] for event in session.events], ["retire"])
        self.assertEqual(delivery.items, [])
        original_outage = failure.cause.api_failure
        for unrelated in (None, ApiFailure("unrelated operation", "dependency unavailable")):
            failure.cause.api_failure = unrelated
            try:
                self.assert_original_raised(controller, selected, failure)
            finally:
                failure.cause.api_failure = original_outage
        actual = gap._executor
        gap._executor = object()
        try:
            with self.assertRaises(ControllerRefused):
                controller.require_held_failure(selected, failure)
        finally:
            gap._executor = actual
        self.assertIs(controller.require_held_failure(selected, failure), failure)

    async def test_returned_science_before_observation_gap_stays_owned_and_unpublished(self):
        selected = pending("a")
        reference = "execution_attempt:sha256:" + "a" * 64
        probe = LiveAttemptProbe()
        session, delivery = Session(probe), RecordingDelivery()
        transport = Transport()
        transport.replies = chain(
            wire_replies(selected, reference)[:-1],
            repeat(reply(b"PRIVATE_POST_RESULT_OUTAGE", status=503)),
        )
        claims = AttemptClaims.live()
        controller = live_controller(
            api(transport, scope().api_origin), probe, claims, session,
            delivery=delivery, on_delivery_retry=Mock(),
            delivery_retry_initial_seconds=0.001, delivery_retry_max_seconds=0.002,
            allowed_job_states=frozenset({"open"}), poll_seconds=0.1,
            task_settle_seconds=0.1,
            observation_recovery=ObservationRecovery(0.1, 0.01, Mock(), Mock()),
        )
        failure = await self.failed_run(controller, selected)
        self.assertIs(type(failure.cause.cause), ObservationGapExpired)
        self.assertIs(failure.cause.api_failure, failure.cause.cause.cause)
        self.assertEqual(failure.cause.api_failure.status, 503)
        self.assertIs(failure.cause.outcome, session.outcome)
        self.assertIs(type(failure.cause.outcome), AnalysisSuccess)
        self.assertTrue(controller.work_settled)
        self.assertFalse(controller.shutdown_ready)
        self.assert_original_raised(controller, selected, failure)
        self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
        self.assertEqual(delivery.items, [])
        self.assertTrue(claims.owns_job(selected.scope, selected.input.job_ref))
        self.assertIs(controller.operation._outcome, session.outcome)

    async def test_gap_retirement_cannot_hide_nested_transport_cleanup_failure(self):
        nested = OSError(13, "PRIVATE_TRANSPORT_CLEANUP")
        for with_cleanup, cleanup in ((False, ()), (True, (nested,))):
            with self.subTest(with_cleanup=with_cleanup):
                exchange = io_failure("Scripted interrupted read", primary=TimeoutError(), stage="read",
                                      cleanup_failures=cleanup)
                started = asyncio.Event()

                class DelayedRetirement(Transport):
                    async def exchange(self, request, *, maximum_bytes, owner=None):
                        if request.method == "GET":
                            self.calls.append((request, maximum_bytes))
                            self.pending_owner, self.retirement_confirmed = owner, False
                            self.last_failure = exchange
                            raise exchange
                        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

                    async def retire(self, *, owner=None):
                        if self.pending_owner is not None:
                            started.set()
                            try:
                                await asyncio.Event().wait()
                            finally:
                                self.pending_owner, self.retirement_confirmed = None, True
                        else:
                            await super().retire(owner=owner)

                selected = pending("a")
                reference = "execution_attempt:sha256:" + "a" * 64
                transport = DelayedRetirement(reply(start_response(
                    job_ref=selected.input.job_ref, execution_attempt_ref=reference,
                )))
                controller, _, delivery, _ = configured(
                    AttemptClaims.live(), selected, reference, AnalysisOutcomeUnresolved("must not execute"),
                    transport=transport, recovery=ObservationRecovery(0.1, 0.01, Mock(), Mock()),
                )
                failure = await self.failed_run(controller, selected)
                self.assertTrue(started.is_set())
                self.assertIs(type(failure.cause.cause), ObservationGapExpired)
                self.assertTrue(controller.shutdown_ready)
                self.assertEqual(failure.cause.cleanup_failures, ())
                self.assertIs(failure.cause.api_failure, failure.cause.cause.cause)
                self.assertIs(failure.cause.api_failure.cause, exchange)
                self.assertEqual(controller.api.can_retry_read(failure.cause.api_failure), not with_cleanup)
                if with_cleanup:
                    self.assertIn(nested, exchange.cleanup_failures)
                    self.assert_original_raised(controller, selected, failure)
                else:
                    self.assertIs(controller.require_held_failure(selected, failure), failure)
                self.assertEqual(delivery.items, [])

    async def test_unrelated_control_interruption_and_pending_outcome_cannot_continue(self):
        selected = pending("a")
        controller, session, _, _ = configured(AttemptClaims.live(), selected,
            "execution_attempt:sha256:" + "a" * 64, AnalysisOutcomeUnresolved("No outcome"))
        failure = await self.failed_run(controller, selected)
        execution = failure.cause
        for field, value in (("api_failure", ApiFailure("unrelated write", "unconfirmed")),
                             ("interruption_cause", asyncio.CancelledError("real interruption"))):
            original = getattr(execution, field)
            setattr(execution, field, value)
            try:
                self.assert_original_raised(controller, selected, failure)
            finally:
                setattr(execution, field, original)
        executor = controller.operation
        executor._outcome = session.outcome
        try:
            self.assertFalse(controller.shutdown_ready)
            self.assert_original_raised(controller, selected, failure)
        finally:
            executor._outcome = None
        self.assertIs(controller.require_held_failure(selected, failure), failure)

    async def test_late_read_has_no_invented_outage_and_keeps_the_job_held(self):
        selected = pending("a")
        reference = "execution_attempt:sha256:" + "a" * 64

        class LateRead(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if request.method == "GET":
                    time.sleep(0.12)
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = LateRead(*wire_replies(selected, reference))
        controller, session, delivery, _ = configured(
            AttemptClaims.live(), selected, reference, AnalysisOutcomeUnresolved("must not execute"),
            transport=transport, recovery=ObservationRecovery(0.1, 0.01, Mock(), Mock()),
        )
        failure = await self.failed_run(controller, selected)
        gap = failure.cause.cause
        self.assertIs(type(gap), ObservationGapExpired)
        self.assertTrue(gap.read_started)
        self.assertIsNone(gap.cause)
        self.assertIsNone(failure.cause.api_failure)
        self.assertIs(controller.require_held_failure(selected, failure), failure)
        self.assertEqual([event[0] for event in session.events], ["retire"])
        self.assertEqual(delivery.items, [])

    async def test_cancelled_observer_and_requested_stop_do_not_become_an_unresolved_hold(self):
        selected = pending("a")
        reference = "execution_attempt:sha256:" + "a" * 64
        probe = LiveAttemptProbe()
        session, delivery = PausedSession(probe, "execute"), RecordingDelivery()
        transport = Transport()
        transport.replies = chain(wire_replies(selected, reference), repeat(reply(observation(
            job_ref=selected.input.job_ref, execution_attempt_ref=reference,
        ))))
        controller = live_controller(
            api(transport, scope().api_origin), probe, AttemptClaims.live(), session,
            delivery=delivery, on_delivery_retry=Mock(),
            delivery_retry_initial_seconds=0.001, delivery_retry_max_seconds=0.002,
            allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.1,
        )
        observer = asyncio.create_task(controller.run(selected))
        try:
            await asyncio.wait_for(session.paused.wait(), 1)
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
            controller.request_stop()
            with self.assertRaises(ControllerFailed) as caught:
                await asyncio.wait_for(controller.join(), 1)
            with self.assertRaises(ControllerRefused):
                controller.require_held_failure(selected, caught.exception)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertEqual(delivery.items, [])
        finally:
            session.release.set()
            controller.request_stop()
            if not observer.done():
                observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            if controller._driver is not None:
                await asyncio.gather(controller._driver, return_exceptions=True)

    async def test_unclassified_failure_and_unreviewed_marker_subclass_are_not_continuation_cases(self):
        class ForeignUnresolved(AnalysisOutcomeUnresolved):
            pass

        for leaf in (RuntimeError("unclassified science failure"), ForeignUnresolved("foreign marker")):
            with self.subTest(kind=type(leaf).__name__):
                selected = pending("a")
                controller, _, _, _ = configured(AttemptClaims.live(), selected,
                    "execution_attempt:sha256:" + "a" * 64, leaf)
                failure = await self.failed_run(controller, selected)
                with self.assertRaises(ControllerRefused):
                    controller.require_held_failure(selected, failure)
