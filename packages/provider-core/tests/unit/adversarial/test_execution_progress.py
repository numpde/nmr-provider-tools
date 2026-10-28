"""Phase delivery uncertainty cannot waive stop, cleanup or condition receipts."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from tests.fakes.execution import live_executor
import unittest
from unittest.mock import Mock, patch

from provider_core.api import ApiFailure
from provider_core.execution import ExecutionFailed, ExecutionStopped
from provider_core.transport import ExchangeFailed
from tests.fakes.api import reply
from tests.fakes.attempts import observation
from tests.fakes.conditions import ConditionTransport, ReportingSession
from tests.fakes.execution import Session, live_attempt
from tests.fakes.phase_progress import PhaseProgressTransport
from tests.fakes.api import api


class PhaseProgressFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_stop_prevents_the_dependent_phase_effect(self):
        for phase in ("preparing", "running"):
            for state in ({"job_state": "cancelled"}, {"state": "succeeded"}):
                with self.subTest(phase=phase, state=state):
                    store, acknowledged, claim = await live_attempt()
                    transport = PhaseProgressTransport(phase)
                    transport.fresh_observation = reply(observation(**state))
                    session, notice = Session(store), Mock()
                    executor = live_executor(
                        api(transport, store.scope.api_origin),
                        store,
                        claim,
                        session,
                        delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                        poll_seconds=1,
                        task_settle_seconds=0.1,
                        on_unconfirmed_phase_progress=notice,
                    )
                    with self.assertRaises(ExecutionFailed) as caught:
                        await executor.run(acknowledged)
                    self.assertIs(type(caught.exception.cause), ExecutionStopped)
                    self.assertIs(caught.exception.cause.__context__, notice.call_args.args[2])
                    # silent-defaults: allow-contract -- Only phases reached before the injected stop belong in the expected event order.
                    expected = ["retire"] if phase == "preparing" else ["prepare", "retire"]
                    self.assertEqual([event[0] for event in session.events], expected)
                    self.assertEqual(store.records, (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
                    notice.assert_called_once()
                    claim.release(owner=claim.lifecycle_owner)

    async def test_failed_followup_read_preserves_both_operation_failures(self):
        store, acknowledged, claim = await live_attempt()
        transport = PhaseProgressTransport("preparing")
        transport.fresh_observation = reply(b"PRIVATE read response", status=503)
        session, notice = Session(store), Mock()
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=1,
            task_settle_seconds=0.1,
            on_unconfirmed_phase_progress=notice,
        )
        with self.assertRaises(ExecutionFailed) as caught:
            await executor.run(acknowledged)
        progress_failure = notice.call_args.args[2]
        read_failure = caught.exception.cause
        self.assertIs(type(read_failure), ApiFailure)
        self.assertIs(read_failure.__context__, progress_failure)
        self.assertIs(caught.exception.api_failure, read_failure)
        self.assertEqual(caught.exception.cleanup_failures, ())
        self.assertEqual([event[0] for event in session.events], ["retire"])
        claim.release(owner=claim.lifecycle_owner)

    async def test_callback_or_original_cleanup_failure_stops_before_a_followup_read(self):
        for failing_owner in ("notice", "retirement"):
            with self.subTest(owner=failing_owner):
                store, acknowledged, claim = await live_attempt()
                transport = PhaseProgressTransport("preparing")
                session = Session(store)
                fault = OSError("Authored independent sink/cleanup failure")
                notice = Mock(side_effect=fault) if failing_owner == "notice" else Mock()
                client = api(transport, store.scope.api_origin)
                executor = live_executor(
                    client,
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                    on_unconfirmed_phase_progress=notice,
                )
                with patch.object(client, "retire", wraps=client.retire) as retire:
                    if failing_owner == "retirement":
                        retire.side_effect = fault
                    with self.assertRaises(ExecutionFailed) as caught:
                        await executor.run(acknowledged)
                self.assertIs(caught.exception.cause, fault)
                self.assertIs(fault.__context__, client.last_failure)
                self.assertEqual(transport.fresh_reads, 0)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                if not transport.retirement_confirmed:
                    await executor.settle()
                claim.release(owner=claim.lifecycle_owner)

    async def test_unreviewed_phase_failure_and_default_policy_remain_fail_stop(self):
        for observer, status in ((None, 503), (None, 401), (Mock(), 401)):
            with self.subTest(opt_in=observer is not None, status=status):
                store, acknowledged, claim = await live_attempt()
                transport = PhaseProgressTransport("preparing")
                transport.phase_failure = reply(b"PRIVATE refusal", status=status)
                session = Session(store)
                executor = live_executor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                    on_unconfirmed_phase_progress=observer,
                )
                with self.assertRaises(ExecutionFailed):
                    await executor.run(acknowledged)
                if observer is not None:
                    observer.assert_not_called()
                self.assertEqual(transport.fresh_reads, 0)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                if not transport.retirement_confirmed:
                    await executor.settle()
                claim.release(owner=claim.lifecycle_owner)

    async def test_condition_report_still_requires_its_matching_acknowledgement(self):
        store, acknowledged, claim = await live_attempt()
        transport = ConditionTransport()
        transport.release.set()
        transport.failure = ExchangeFailed("Condition receipt unavailable", primary=TimeoutError())
        session, notice = ReportingSession(store), Mock()
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=1,
            task_settle_seconds=0.1,
            on_unconfirmed_phase_progress=notice,
        )
        with self.assertRaises(ExecutionFailed):
            await executor.run(acknowledged)
        notice.assert_not_called()
        self.assertEqual([event[0] for event in session.events], ["retire"])
        self.assertEqual(store.records, (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
        claim.release(owner=claim.lifecycle_owner)
