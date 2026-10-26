"""The controller forwards phase-delivery policy to its owned executor."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.controller import AttemptController
from provider_core.ownership import AttemptClaims
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation
from tests.fakes.controller import empty_store, lifecycle_replies
from tests.fakes.execution import Session
from tests.fakes.api import api


class ControllerPhaseProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase_delivery_policy_reaches_the_executor_without_repeating_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, notice = Session(store), Mock()
            replies = list(lifecycle_replies())
            replies[3] = reply(b"PRIVATE progress response", status=503)
            replies.insert(4, reply(observation()))
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
                    on_unconfirmed_phase_progress=notice,
                ).run(record())
                self.assertIs(type(confirmed), DeliveryAcceptedCheckpoint)
                notice.assert_called_once()
                attempt, phase, failure = notice.call_args.args
                self.assertEqual(attempt.start, record())
                self.assertEqual((phase, failure.status), ("preparing", 503))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(len(transport.calls), 9)
                self.assertEqual(await store.inventory(), ())
            finally:
                await store.close()
