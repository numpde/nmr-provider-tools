"""A current awaited condition recovers only before a matching acknowledgement."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import json
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.attempt_record import LocalExecutionPhase, TerminalOperation
from provider_core.execution import AttemptExecutor, ConditionRecovery
from tests.fakes.api import reply
from tests.fakes.condition_recovery import ConditionOutageTransport
from tests.fakes.conditions import ReportingSession
from tests.fakes.execution import acknowledged_store
from tests.fakes.api import api


class ConditionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_and_clear_recover_without_repeating_dependent_effects(self):
        for failed_put in (2, 3, 5, 6):
            with self.subTest(failed_put=failed_put):
                await self.check_current_condition(failed_put)

    async def check_current_condition(self, failed_put):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=failed_put)
            # Two failed sends make the capped doubling/backoff observable by
            # the existing monitor, without a second reader in the report port.
            transport.faults = iter((reply(b"PRIVATE first", status=503), reply(b"PRIVATE second", status=503)))
            session = ReportingSession(store)
            notices, recovered = [], Mock()

            def retry(attempt, phase, condition, failure, seconds):
                self.assertEqual(attempt.start, acknowledged.start)
                self.assertIs(attempt.admission, acknowledged.admission)
                self.assertIs(
                    attempt.local_phase,
                    LocalExecutionPhase.NOT_ENTERED if failed_put < 4 else LocalExecutionPhase.EXECUTION_ENTERED,
                )
                self.assertIn(phase, ("preparing", "running"))
                self.assertEqual(condition, transport.condition)
                self.assertEqual(failure.status, 503)
                self.assertTrue(transport.retirement_confirmed)
                self.assertGreater(seconds, 0)
                self.assertLessEqual(seconds, 0.04)
                # silent-defaults: allow-contract -- The injected fault point determines which phases have already happened.
                expected = [] if failed_put < 4 else ["prepare"]
                self.assertEqual([event[0] for event in session.events], expected)
                notices.append(failure)

            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.04, retry, recovered),
            )
            try:
                terminal = await asyncio.wait_for(executor.run(acknowledged), 3)
                self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
                self.assertEqual(len(notices), 2)
                # silent-defaults: allow-contract -- The injected fault point determines which phases have already happened.
                recovered.assert_called_once_with(
                    acknowledged if failed_put < 4 else executor._record,
                    "preparing" if failed_put < 4 else "running",
                    transport.condition,
                    notices[0],
                )
                self.assertTrue(transport.read_after_failure.is_set())
                requests = transport.condition_requests
                self.assertEqual(len(requests), 3)
                self.assertEqual({request.content for request in requests}, {transport.condition_body})
                self.assertEqual(len({request.headers["signature-input"] for request in requests}), 3)
                self.assertEqual(len({request.headers["signature"] for request in requests}), 3)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(await store.inventory(), (terminal,))
                # silent-defaults: allow-contract -- Only an observed condition acknowledgement is retained; absence remains None.
                self.assertEqual(
                    json.loads(requests[0].content)["condition_code"], None if failed_put in (3, 6) else "waiting_capacity"
                )
                self.assertIsNone(executor.last_failure)
                claim.release()
            finally:
                await store.close()

    def test_policy_refuses_unbounded_values_or_missing_observers(self):
        for seconds in (0, -1, float("inf"), float("nan"), True, "1"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                ConditionRecovery(seconds, 1, Mock(), Mock())
            with self.subTest(backoff=seconds), self.assertRaises(ValueError):
                ConditionRecovery(1, seconds, Mock(), Mock())
        for observers in ((None, Mock()), (Mock(), None)):
            with self.subTest(observers=observers), self.assertRaises(ValueError):
                ConditionRecovery(1, 1, *observers)
