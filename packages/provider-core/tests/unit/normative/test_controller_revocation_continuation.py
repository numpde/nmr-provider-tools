"""A service can settle an original server stop without restarting its Attempt."""

import asyncio
import unittest

from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.job_input import response
from tests.fakes.live_analysis import LiveSession
from provider_core.job_input import verify_job_input
from tests.unit.normative import test_live_controller as live_fixture


class RevocationContinuationTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, session, *, stop=None):
        if stop is None:
            stop = {"job_state": "cancelled"}
        fixture = live_fixture.LiveControllerTests()
        return fixture.controller(
            Transport(reply(start_response()), reply(observation()), reply(progress_response()),
                      reply(observation(**stop))),
            claims=AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=2)),
            session=session, poll_seconds=0.01,
            preverified_input=verify_job_input(response(), feed=record().input),
        )

    async def stopped(self, controller):
        with self.assertRaises(ControllerFailed) as caught:
            await asyncio.wait_for(controller.run(record()), 1)
        return caught.exception

    async def test_settled_cancellation_and_expiry_have_one_checked_continuation(self):
        for stop in ({"job_state": "cancelled"}, {"state": "expired"}):
            with self.subTest(stop=stop):
                session = PendingPreparation()
                controller = self.controller(session, stop=stop)
                failure = await self.stopped(controller)
                observed = await controller.settle_revocation(failure)
                self.assertIs(observed, failure.cause.cause.observation)
                self.assertIs(observed, await controller.settle_revocation(failure))
                self.assertEqual((), controller._claims.live_owners())
                self.assertEqual(["prepare", "retire"], session.events)
                self.assertEqual([], controller.delivery.items)
                self.assertIs(failure, controller.last_failure)

    async def test_late_retirement_stays_owned_until_the_original_resource_closes(self):
        session = PendingPreparation()
        session.release.clear()
        controller = self.controller(session)
        continuation = None
        try:
            failure = await self.stopped(controller)
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            continuation = asyncio.create_task(controller.settle_revocation(failure))
            await asyncio.sleep(0.08)  # Exceed the original settlement observation allowance.
            self.assertFalse(continuation.done())
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            self.assertTrue(session.retiring.is_set())
            self.assertEqual(["prepare"], session.events)
            session.release.set()
            observed = await asyncio.wait_for(continuation, 1)
            self.assertIs(observed, failure.cause.cause.observation)
            self.assertEqual((), controller._claims.live_owners())
            self.assertEqual(["prepare", "retire"], session.events)
        finally:
            session.release.set()
            if continuation is not None:
                await asyncio.gather(continuation, return_exceptions=True)
            await asyncio.gather(controller.settle(), return_exceptions=True)

    async def test_cancelling_the_continuation_wait_preserves_its_original_cleanup(self):
        session = PendingPreparation()
        session.release.clear()
        controller = self.controller(session)
        try:
            failure = await self.stopped(controller)
            continuation = asyncio.create_task(controller.settle_revocation(failure))
            await asyncio.sleep(0)
            continuation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await continuation
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            self.assertFalse(controller.shutdown_ready)
            self.assertFalse(controller.operation._retirement_task.cancelled())
            session.release.set()
            observed = await controller.settle_revocation(failure)
            self.assertIs(observed, failure.cause.cause.observation)
            self.assertEqual(["prepare", "retire"], session.events)
            self.assertEqual((), controller._claims.live_owners())
        finally:
            session.release.set()
            await asyncio.gather(controller.settle(), return_exceptions=True)

    async def test_foreign_failure_cannot_use_another_controllers_revocation(self):
        first = self.controller(PendingPreparation())
        second = self.controller(PendingPreparation())
        original = await self.stopped(first)
        foreign = await self.stopped(second)
        self.assertIsNone(await first.settle_revocation(foreign))
        self.assertIsNone(await first.settle_revocation(ControllerFailed(original.cause, ())))
        self.assertIs(original.cause.cause.observation, await first.settle_revocation(original))

    async def test_failed_retirement_is_not_retried_as_a_harmless_wait(self):
        class FailedRetirement(PendingPreparation):
            async def retire(self):
                self.events.append("retire")
                raise TimeoutError("The resource owner failed independently")

        session = FailedRetirement()
        controller = self.controller(session)
        failure = await self.stopped(controller)
        self.assertIsNone(await controller.settle_revocation(failure))
        self.assertEqual(["prepare", "retire"], session.events)
        self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
        self.assertIs(failure, controller.last_failure)

    async def test_an_independent_late_retirement_failure_remains_a_failure(self):
        class FailedLateRetirement(PendingPreparation):
            async def retire(self):
                self.events.append("retire")
                self.retiring.set()
                await self.release.wait()
                raise OSError("Original resource could not close")

        session = FailedLateRetirement()
        session.release.clear()
        controller = self.controller(session)
        continuation = None
        try:
            failure = await self.stopped(controller)
            continuation = asyncio.create_task(controller.settle_revocation(failure))
            await asyncio.sleep(0)
            session.release.set()
            with self.assertRaises(ControllerFailed) as caught:
                await continuation
            self.assertIs(failure, caught.exception)
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            self.assertEqual(["prepare", "retire"], session.events)
            self.assertTrue(any(isinstance(error, OSError) for error in failure.cause.cleanup_failures))
        finally:
            session.release.set()
            if continuation is not None:
                await asyncio.gather(continuation, return_exceptions=True)


class PendingPreparation(LiveSession):
    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.events.append("prepare")
        await asyncio.Future()
