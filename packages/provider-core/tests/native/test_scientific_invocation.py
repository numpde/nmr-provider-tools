"""An invocation retains original work and settles it before resources can close."""

import asyncio
import unittest

from provider_core.scientific_invocation import open_invocation


class ScientificInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_observer_rejoins_one_original_sequence(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        scientific_result = object()

        async def calculate():
            calls.append("calculate")
            entered.set()
            await release.wait()

            return scientific_result

        async with open_invocation(calculate) as invocation:
            waiter = asyncio.create_task(invocation.wait())

            await entered.wait()
            waiter.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await waiter

            release.set()
            self.assertIs(scientific_result, await invocation.wait())
            self.assertIs(scientific_result, await invocation.wait())

        self.assertEqual(["calculate"], calls)

    async def test_repeated_close_and_cancelled_closer_join_original_cleanup(self):
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def calculate():
            entered.set()

            try:
                await asyncio.Event().wait()
            finally:
                events.append("cleanup started")
                cleaning.set()
                await release.wait()
                events.append("cleanup finished")

        async with open_invocation(calculate) as invocation:
            await entered.wait()
            closer = asyncio.create_task(invocation.close())

            await cleaning.wait()
            closer.cancel("interrupted shutdown observer")
            second_closer = asyncio.create_task(invocation.close())

            await asyncio.sleep(0)
            closer.cancel("interrupted again")
            await asyncio.sleep(0)
            self.assertFalse(closer.done())
            self.assertFalse(second_closer.done())
            release.set()

            with self.assertRaises(BaseExceptionGroup) as raised:
                await closer

            self.assertTrue(all(isinstance(cause, asyncio.CancelledError) for cause in raised.exception.exceptions))
            await second_closer

        self.assertEqual(["cleanup started", "cleanup finished"], events)

    async def test_body_failure_and_independent_cleanup_failure_survive(self):
        entered = asyncio.Event()
        body_failure = ValueError("Cannot publish the report")
        cleanup_failure = OSError("Cannot settle the scientific resource")

        async def calculate():
            entered.set()

            try:
                await asyncio.Event().wait()
            finally:
                raise cleanup_failure

        with self.assertRaises(BaseExceptionGroup) as raised:
            async with open_invocation(calculate):
                await entered.wait()

                raise body_failure

        self.assertEqual((body_failure, cleanup_failure), raised.exception.exceptions)

    async def test_repeated_failure_observation_and_context_exit_do_not_duplicate_failure(self):
        failure = OSError("Calculation failed")
        calls = []

        async def calculate():
            calls.append("calculate")

            raise failure

        with self.assertRaises(OSError) as raised:
            async with open_invocation(calculate) as invocation:
                for _ in range(2):
                    with self.assertRaises(OSError) as observed:
                        await invocation.wait()

                    self.assertIs(failure, observed.exception)

                await invocation.wait()

        self.assertIs(failure, raised.exception)
        self.assertEqual(["calculate"], calls)

    async def test_independent_task_cancellation_is_not_successful_close(self):
        entered = asyncio.Event()

        async def calculate():
            entered.set()

            raise asyncio.CancelledError("Scientific sequence cancelled itself")

        with self.assertRaises(asyncio.CancelledError):
            async with open_invocation(calculate):
                await entered.wait()

    async def test_immediate_close_prevents_pending_sequence_from_starting(self):
        calls = []

        async def calculate():
            calls.append("calculate")

        async with open_invocation(calculate) as invocation:
            await invocation.close()

        self.assertEqual([], calls)

    async def test_process_control_failure_is_contained_and_preserved(self):
        failure = SystemExit("Scientific sequence requested exit")

        async def calculate():
            raise failure

        with self.assertRaises(BaseExceptionGroup) as raised:
            async with open_invocation(calculate) as invocation:
                await invocation.wait()

        self.assertEqual((failure,), raised.exception.exceptions)
