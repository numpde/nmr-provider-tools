"""A consumer's Job policy cannot authorize a terminal Attempt or unknown state."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from tests.fakes.execution import live_executor
import unittest

from provider_core.attempt_response import AttemptObservation
from provider_core.execution import ExecutionFailed, ExecutionStopped
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, observation, progress_response
from tests.fakes.execution import PausedSession, Session, live_attempt, execute_with_watchdog
from tests.fakes.job_input import response
from tests.fakes.api import api


class ExecutionJobPolicyFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_cancelled_empty_or_mutable_policies_are_refused_before_claim_ownership(self):
        store, _, claim = await live_attempt()
        for states in (None, "open", {"open"}, ["open"], frozenset(), frozenset({"unknown"}),
                       frozenset({"open", "unknown"}), frozenset({1}),
                       frozenset({"cancelled"}), frozenset({"open", "cancelled"})):
            with self.subTest(states=states), self.assertRaises(ValueError):
                live_executor(api(Transport(), store.scope.api_origin), store, claim, Session(store),
                                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=states, poll_seconds=0.01, task_settle_seconds=0.1)
        claim.release(owner=claim.lifecycle_owner)

    async def test_every_terminal_attempt_stops_even_when_consumer_permits_closed_jobs(self):
        for state in ("succeeded", "failed", "expired"):
            for job_state in ("open", "closed", "cancelled"):
                with self.subTest(state=state, job_state=job_state):
                    store, acknowledged, claim = await live_attempt()
                    session = Session(store)
                    transport = Transport(reply(observation(state=state, job_state=job_state)))
                    executor = live_executor(api(transport, store.scope.api_origin), store, claim, session,
                                               delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open", "closed"}),
                                               poll_seconds=0.01, task_settle_seconds=0.1)
                    with self.assertRaises(ExecutionFailed) as failed:
                        await executor.run(acknowledged)
                    self.assertIsInstance(failed.exception.cause, ExecutionStopped)
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    self.assertEqual(store.records, (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
                    self.assertEqual(len(transport.calls), 1)
                    claim.release(owner=claim.lifecycle_owner)

    async def test_closed_job_under_open_only_policy_and_cancelled_job_under_secs_policy_stop_each_phase(self):
        for phase in ("prepare", "execute"):
            for states, stopped_state in ((frozenset({"open"}), "closed"), (frozenset({"open", "closed"}), "cancelled")):
                with self.subTest(phase=phase, states=states, stopped_state=stopped_state):
                    await self._stop_disallowed_job(phase, states, stopped_state)

    async def _stop_disallowed_job(self, phase, states, stopped_state):
        store, acknowledged, claim = await live_attempt()
        session = PausedSession(store, phase)
        transport = Transport(reply(response()), reply(progress_response()), reply(progress_response(phase="running")))
        client = api(transport, store.scope.api_origin)

        async def read_attempt(*_args, **_kwargs):
            # silent-defaults: allow-contract -- The fixture Job changes state only after its observed pause.
            job_state = stopped_state if session.paused.is_set() else "open"
            return AttemptObservation(ATTEMPT_REF, "job:example", "in_progress", job_state)

        client.read_attempt = read_attempt
        executor = live_executor(client, store, claim, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=states,
                                   poll_seconds=0.01, task_settle_seconds=0.1)
        try:
            with self.assertRaises(ExecutionFailed) as failed:
                await execute_with_watchdog(executor, acknowledged)
            self.assertIsInstance(failed.exception.cause, ExecutionStopped)
            self.assertEqual(failed.exception.cause.observation.job_state, stopped_state)
            self.assertIsNone(failed.exception.outcome)
            # silent-defaults: allow-contract -- The stopped phase determines which events have already happened.
            self.assertEqual([event[0] for event in session.events],
                             ["prepare", "retire"] if phase == "prepare" else ["prepare", "execute", "retire"])
            claim.release(owner=claim.lifecycle_owner)
        finally:
            session.release.set()
