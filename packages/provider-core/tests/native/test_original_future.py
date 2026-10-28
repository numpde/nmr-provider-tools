"""Provider-owned thread results retain their original exception and inputs."""

import asyncio
from concurrent.futures import CancelledError, Future
import time
import unittest

from provider_core.async_runner_session import await_original


class OriginalFutureTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_value_is_returned_without_repeating_work(self):
        original = Future()
        value = object()
        original.set_result(value)
        self.assertIs(value, await await_original(original, deadline=None))

    async def test_exact_thread_failure_and_notes_survive_repeated_observation(self):
        for failure in (TimeoutError("native timeout"), asyncio.CancelledError("native interruption"),
                        KeyboardInterrupt("native interrupt"), SystemExit(7)):
            with self.subTest(kind=type(failure).__name__):
                original = Future()
                failure.add_note("original thread evidence")
                asyncio.get_running_loop().call_soon(original.set_exception, failure)
                for _ in range(2):
                    with self.assertRaises(type(failure)) as caught:
                        await await_original(original, deadline=time.monotonic() + 1)
                    self.assertIs(failure, caught.exception)
                    self.assertEqual(["original thread evidence"], caught.exception.__notes__)
                await asyncio.sleep(0)

    async def test_wait_expiry_retains_the_original_for_its_later_result(self):
        original = Future()
        with self.assertRaises(TimeoutError):
            await await_original(original, deadline=time.monotonic() + 0.01)
        self.assertFalse(original.done())
        original.set_result("later result")
        self.assertEqual("later result", await await_original(original, deadline=None))

    async def test_cancelled_waiter_leaves_original_work_joinable(self):
        original = Future()
        observer = asyncio.create_task(await_original(original, deadline=None))
        await asyncio.sleep(0)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertFalse(original.done())
        original.set_result("original result")
        self.assertEqual("original result", await await_original(original, deadline=None))

    async def test_cancelled_physical_future_keeps_its_ordinary_cancellation(self):
        original = Future()
        self.assertTrue(original.cancel())
        with self.assertRaises(CancelledError):
            await await_original(original, deadline=None)

    async def test_async_future_is_not_an_original_thread_owner(self):
        with self.assertRaises(TypeError):
            await await_original(asyncio.get_running_loop().create_future(), deadline=None)
