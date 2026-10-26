"""Controller-selected condition recovery keeps matching receipt before effects."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.controller import AttemptController
from provider_core.execution import ConditionRecovery
from provider_core.ownership import AttemptClaims
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import progress_response
from tests.fakes.conditions import ReportingSession
from tests.fakes.controller import empty_store, lifecycle_replies
from tests.fakes.api import api


class ControllerConditionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_passes_the_selected_condition_policy_to_its_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = ReportingSession(store)
            session.conditions = {"prepare": ("waiting_capacity",), "execute": ()}
            retry, recovered = Mock(), Mock()
            replies = list(lifecycle_replies())
            replies[4:4] = [reply(b"PRIVATE outage", status=503), reply(progress_response(condition_code="waiting_capacity"))]
            transport = Transport(*replies)
            try:
                confirmed = await AttemptController(
                    api(transport, scope().api_origin),
                    store,
                    AttemptClaims(),
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                    condition_recovery=ConditionRecovery(1, 0.01, retry, recovered),
                ).run(record())
                self.assertIs(type(confirmed), DeliveryAcceptedCheckpoint)
                retry.assert_called_once()
                recovered.assert_called_once()
                self.assertEqual(retry.call_args.args[1:3], ("preparing", "waiting_capacity"))
                self.assertIs(retry.call_args.args[3], recovered.call_args.args[3])
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(await store.inventory(), ())
                self.assertEqual(len(transport.calls), 10)
            finally:
                await store.close()
