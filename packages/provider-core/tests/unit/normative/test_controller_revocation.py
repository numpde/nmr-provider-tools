"""A returned scientific handle stays owned until its exact session retires."""

import asyncio
import unittest
from unittest.mock import Mock

from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.execution import ExecutionStopped
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.live_analysis import LiveSession


class PreparedHandleSession(LiveSession):
    """Record the exact handle crossing preparation and retirement boundaries."""

    def __init__(self):
        super().__init__()
        self.returned_handle = None
        self.retired_handle = None
        self.retirement_calls = 0

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.returned_handle = await super().prepare(
            acknowledged, instructions, report_condition=report_condition,
        )
        return self.returned_handle

    async def retire(self):
        self.retirement_calls += 1
        await super().retire()
        self.retired_handle = self.returned_handle


def controller_for(transport, claims, session):
    """Give the real controller explicit API, relay, preparation and stop owners."""
    return AttemptController.live(
        api(transport, scope().api_origin), claims, session,
        scope=scope(), stopping=asyncio.Event(),
        start_policy=LiveStartPolicy(cadence_seconds=0.01, on_retry=Mock(), on_recovered=Mock()),
        preverified_input=verify_job_input(response(), feed=record().input),
        delivery=RecordingDelivery(), on_delivery_retry=Mock(),
        allowed_job_states=frozenset({"open"}),
        delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
        poll_seconds=1, task_settle_seconds=0.05,
    )


class PreparedHandleRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_returned_handle_retires_once_before_revocation_releases_capacity(self):
        for stop in ({"job_state": "cancelled"}, {"state": "expired"}):
            for delayed in (False, True):
                with self.subTest(stop=stop, delayed_retirement=delayed):
                    claims, session = AttemptClaims.live(), PreparedHandleSession()
                    session.release.clear()
                    revoked = reply(observation(**stop))
                    transport = Transport(
                        reply(start_response()), reply(observation()),
                        reply(progress_response()), revoked,
                    )
                    controller = controller_for(transport, claims, session)
                    run = asyncio.create_task(controller.run(record()))
                    try:
                        await asyncio.wait_for(session.retiring.wait(), 1)
                        # The fourth exchange is the acceptance read following
                        # the successful return of this exact prepared handle.
                        self.assertEqual(4, len(transport.calls))
                        self.assertIs(session.handle, session.returned_handle)
                        self.assertIsNone(session.retired_handle)
                        self.assertEqual(["prepare"], session.events)
                        self.assertIs(type(controller.operation._checkpoint), AcknowledgedCheckpoint)
                        self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
                        self.assertFalse(controller.work_settled)
                        self.assertIsNone(controller.operation.settled_revocation)
                        if delayed:
                            await asyncio.wait((run,), timeout=1)
                            self.assertTrue(run.done(), "The original stop must remain observable during cleanup")
                            self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
                            self.assertIsNone(controller.operation.settled_revocation)
                    finally:
                        session.release.set()
                        failure, = await asyncio.wait_for(asyncio.gather(run, return_exceptions=True), 1)

                    self.assertIsInstance(failure, ControllerFailed)
                    self.assertIs(type(failure.cause.cause), ExecutionStopped)
                    self.assertIsNone(await controller.settle())
                    self.assertIsNone(await controller.settle())
                    self.assertIs(controller.operation.settled_revocation, failure.cause.cause.observation)
                    self.assertIs(session.handle, session.retired_handle)
                    self.assertEqual(1, session.retirement_calls)
                    self.assertEqual(["prepare", "retire"], session.events)
                    self.assertEqual([], controller.delivery.items)
                    self.assertEqual((), claims.live_owners())

    async def test_revocation_after_entry_retains_ownership_without_dispatching_the_handle(self):
        for stop in ({"job_state": "cancelled"}, {"state": "expired"}):
            with self.subTest(stop=stop):
                claims, session = AttemptClaims.live(), PreparedHandleSession()
                transport = Transport(
                    reply(start_response()), reply(observation()), reply(progress_response()),
                    reply(observation()), reply(progress_response(phase="running")),
                    reply(observation(**stop)),
                )
                controller = controller_for(transport, claims, session)
                with self.assertRaises(ControllerFailed) as stopped:
                    await controller.run(record())
                self.assertIs(type(stopped.exception.cause.cause), ExecutionStopped)
                self.assertIs(type(controller.operation._checkpoint), EnteredCheckpoint)
                for _ in range(2):
                    with self.assertRaises(ControllerFailed):
                        await controller.settle()
                self.assertIsNone(controller.operation.settled_revocation)
                self.assertIs(session.handle, session.returned_handle)
                self.assertIs(session.handle, session.retired_handle)
                self.assertEqual(1, session.retirement_calls)
                self.assertEqual(["prepare", "retire"], session.events)
                self.assertEqual([], controller.delivery.items)
                self.assertEqual((controller,), claims.live_owners())
