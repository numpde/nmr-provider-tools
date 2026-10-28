"""An explicit consumer policy can continue a closed Job's active Attempt."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from tests.fakes.execution import live_executor
import unittest

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_response import AttemptObservation
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, progress_response
from tests.fakes.execution import PausedSession, live_attempt, execute_with_watchdog
from tests.fakes.job_input import response
from tests.fakes.api import api


class ExecutionJobPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_job_can_continue_during_preparation_and_execution_when_consumer_permits_it(self):
        for phase in ("prepare", "execute"):
            with self.subTest(phase=phase):
                await self._continue_closed_job(phase)

    async def _continue_closed_job(self, phase):
        store, acknowledged, claim = await live_attempt()
        session = PausedSession(store, phase)
        client = api(Transport(reply(response()), reply(progress_response()), reply(progress_response(phase="running"))),
                     store.scope.api_origin)
        observed = []

        async def read_attempt(*_args, **_kwargs):
            # silent-defaults: allow-contract -- The fixture Job changes state only after its observed pause.
            state = "closed" if session.paused.is_set() else "open"
            observed.append(state)
            if state == "closed":
                session.release.set()
            return AttemptObservation(ATTEMPT_REF, "job:example", "in_progress", state)

        client.read_attempt = read_attempt
        executor = live_executor(client, store, claim, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open", "closed"}),
                                   poll_seconds=0.01, task_settle_seconds=0.1)
        try:
            terminal = await execute_with_watchdog(executor, acknowledged)
            self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
            self.assertIn("closed", observed)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertEqual(store.records, (terminal,))
            claim.release(owner=claim.lifecycle_owner)
        finally:
            session.release.set()
