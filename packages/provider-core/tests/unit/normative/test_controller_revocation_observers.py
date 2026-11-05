"""Revocation observers share original settlement without owning its cancellation."""

import asyncio
import unittest

from tests.fakes.attempt_store import record, scope
from tests.unit.normative import test_controller_revocation_continuation as revocation_fixture


class RevocationObserverTests(unittest.IsolatedAsyncioTestCase):
    async def pending_revocation(self):
        fixture = revocation_fixture.RevocationContinuationTests()
        session = revocation_fixture.PendingPreparation()
        session.release.clear()
        controller = fixture.controller(session)
        failure = await fixture.stopped(controller)
        self.addAsyncCleanup(self.finish_original, controller, session)
        return controller, session, failure

    async def finish_original(self, controller, session):
        session.release.set()
        await asyncio.gather(controller.settle(), return_exceptions=True)

    async def test_concurrent_observers_receive_the_same_original_revocation(self):
        controller, session, failure = await self.pending_revocation()
        first = asyncio.create_task(controller.settle_revocation(failure))
        second = asyncio.create_task(controller.settle_revocation(failure))
        try:
            await asyncio.sleep(0)
            self.assertFalse(controller.shutdown_ready)
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            session.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 1,
            )
            for result in results:
                self.assertIs(
                    failure.cause.cause.observation, result,
                    "Concurrent observers must share completed revocation, not compete for settlement",
                )
            self.assertEqual((), controller._claims.live_owners())
            self.assertEqual(["prepare", "retire"], session.events)
            self.assertEqual([], controller.delivery.items)
        finally:
            session.release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await asyncio.gather(controller.settle(), return_exceptions=True)

    async def test_cancelling_an_observer_inside_settlement_leaves_recovery_joinable(self):
        controller, session, failure = await self.pending_revocation()
        delivery_retiring, delivery_release = asyncio.Event(), asyncio.Event()
        delivery_interrupted = asyncio.Event()
        original_retire = controller.delivery.retire

        async def held_delivery_retirement():
            controller.delivery.retirement_confirmed = False
            delivery_retiring.set()
            try:
                await delivery_release.wait()
            except asyncio.CancelledError:
                delivery_interrupted.set()
                raise
            await original_retire()

        # Enter the real executor/controller settlement, past the helper's
        # initial wait for the already owned scientific retirement task.
        controller.delivery.retire = held_delivery_retirement
        observer = asyncio.create_task(controller.settle_revocation(failure))
        resumed = None
        try:
            session.release.set()
            await asyncio.wait_for(delivery_retiring.wait(), 1)
            self.assertFalse(controller.shutdown_ready)
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
            self.assertFalse(controller.shutdown_ready)
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            resumed = asyncio.create_task(controller.settle_revocation(failure))
            delivery_release.set()
            observed = await asyncio.wait_for(resumed, 1)
            self.assertIs(
                failure.cause.cause.observation, observed,
                "Cancelling a settlement observer must not disqualify the original revocation",
            )
            self.assertFalse(delivery_interrupted.is_set())
            self.assertEqual((), controller._claims.live_owners())
            self.assertEqual(["prepare", "retire"], session.events)
            self.assertEqual([], controller.delivery.items)
        finally:
            session.release.set()
            delivery_release.set()
            await asyncio.gather(observer, return_exceptions=True)
            if resumed is not None:
                await asyncio.gather(resumed, return_exceptions=True)
            await asyncio.gather(controller.settle(), return_exceptions=True)

    async def test_external_settlement_joins_the_original_revocation_wait(self):
        controller, session, failure = await self.pending_revocation()
        continuation = asyncio.create_task(controller.settle_revocation(failure))
        external = None
        try:
            await asyncio.sleep(0)
            external = asyncio.create_task(controller.settle())
            # Exceed the existing 0.05-second observation allowance. External
            # shutdown observation must join the pending owner, not retry it.
            await asyncio.sleep(0.08)
            self.assertFalse(
                external.done(),
                "External settlement must await the owned revocation instead of timing it out again",
            )
            self.assertFalse(controller.shutdown_ready)
            self.assertTrue(controller._claims.owns_job(scope(), record().input.job_ref))
            session.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(continuation, external, return_exceptions=True), 1,
            )
            self.assertIs(failure.cause.cause.observation, results[0])
            self.assertNotIsInstance(results[1], BaseException)
            self.assertEqual((), controller._claims.live_owners())
            self.assertEqual(["prepare", "retire"], session.events)
            self.assertEqual([], controller.delivery.items)
        finally:
            session.release.set()
            if external is not None:
                await asyncio.gather(external, return_exceptions=True)
            await asyncio.gather(continuation, return_exceptions=True)
            await asyncio.gather(controller.settle(), return_exceptions=True)
