"""Keep shutdown observation attached to the original SDK cleanup control."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from provider_core.hosting import CleanupObservation, Failure, settle_cleanup


class CleanupSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_original_completion_observation(self):
        completed = CleanupObservation(revision=1, settled=True, allowance_expired=False, failures=())
        control = SimpleNamespace(observe=AsyncMock(return_value=completed))
        self.assertIs(expr1=completed, expr2=await settle_cleanup(control=control))

    async def test_original_cleanup_failure_is_not_repackaged(self):
        original = OSError("Original cleanup failed")
        failed = CleanupObservation(
            revision=1,
            settled=False,
            allowance_expired=False,
            failures=(Failure(phase="cleanup", cause=original),),
        )
        control = SimpleNamespace(observe=AsyncMock(return_value=failed))
        with self.assertRaises(expected_exception=OSError) as raised:
            await settle_cleanup(control=control)
        self.assertIs(expr1=original, expr2=raised.exception)

    async def test_overdue_cleanup_keeps_waiting_for_original_completion(self):
        waiting = asyncio.Event()
        finish = asyncio.Event()

        async def observe(*, after, wait_seconds):
            if after is None:
                return CleanupObservation(revision=1, settled=False, allowance_expired=True, failures=())
            waiting.set()
            await finish.wait()
            return CleanupObservation(revision=2, settled=True, allowance_expired=True, failures=())

        control = SimpleNamespace(observe=observe)
        task = asyncio.create_task(coro=settle_cleanup(control=control))
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
                self.assertFalse(expr=task.done())
                finish.set()
                completed = await task
            self.assertTrue(expr=completed.settled)
            self.assertTrue(expr=completed.allowance_expired)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_observer_cancellation_does_not_cancel_cleanup(self):
        waiting = asyncio.Event()
        finish = asyncio.Event()
        completed = []

        async def observe(*, after, wait_seconds):
            waiting.set()
            await finish.wait()
            completed.append(True)
            return CleanupObservation(revision=1, settled=True, allowance_expired=False, failures=())

        task = asyncio.create_task(coro=settle_cleanup(control=SimpleNamespace(observe=observe)))
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
                task.cancel("first interruption")
                loop = asyncio.get_running_loop()
                loop.call_soon(task.cancel, "second interruption")
                loop.call_soon(finish.set)
                with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
                    await task
            self.assertEqual(first=2, second=len(raised.exception.exceptions))
            self.assertTrue(expr=all(isinstance(cause, asyncio.CancelledError) for cause in raised.exception.exceptions))
            self.assertEqual(first=[True], second=completed)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failure_and_observer_interruption_keep_both_original_causes(self):
        waiting = asyncio.Event()
        finish = asyncio.Event()
        original = OSError("The original cleanup failed after interruption")

        async def observe(*, after, wait_seconds):
            waiting.set()
            await finish.wait()
            return CleanupObservation(
                revision=1,
                settled=False,
                allowance_expired=False,
                failures=(Failure(phase="cleanup", cause=original),),
            )

        task = asyncio.create_task(coro=settle_cleanup(control=SimpleNamespace(observe=observe)))
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
                task.cancel("observer interrupted")
                finish.set()
                with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
                    await task
            self.assertIsInstance(obj=raised.exception.exceptions[0], cls=asyncio.CancelledError)
            self.assertIs(expr1=original, expr2=raised.exception.exceptions[1])
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
