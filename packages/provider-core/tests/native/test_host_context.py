"""Exercise host lifetime scopes against actual SDK retirement and native threads."""

import asyncio
from dataclasses import dataclass
from threading import Event
import unittest
from unittest.mock import patch

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import settle_cleanup
from provider_core.scientific import Contribution, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Inspection:
    value: int


class OwnedScience(Contribution):
    def __init__(self, *, close):
        super().__init__()
        self.close_resource = close

    @operation()
    def inspect(self) -> Inspection:
        return Inspection(value=0)

    def close(self) -> None:
        self.close_resource()


def host() -> InProcessExecutionHost:
    return InProcessExecutionHost(
        input_bytes=4096,
        result_bytes=4096,
        state_values=1,
        progress_records=1,
        initialization_seconds=5,
        retirement_seconds=5,
    )


class HostContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_used_contribution_is_retired_once_on_exit(self):
        closed = []

        def close():
            closed.append(True)

        async with host() as owner:
            owner.adopt(contribution=OwnedScience(close=close), requirements=frozenset())
            self.assertEqual(first=[], second=closed)

        observation = await owner.retire().observe(after=None, wait_seconds=0)

        self.assertTrue(expr=observation.settled)
        self.assertEqual(first=[True], second=closed)

    async def test_body_error_remains_original_after_retirement(self):
        original = OSError("Provider assembly failed")

        with self.assertRaises(expected_exception=OSError) as raised:
            async with host() as owner:
                raise original

        self.assertIs(expr1=original, expr2=raised.exception)
        observation = await owner.retire().observe(after=None, wait_seconds=0)

        self.assertTrue(expr=observation.settled)

    async def test_synchronous_retirement_request_failure_keeps_body_cause(self):
        original = ValueError("Provider work failed")
        cleanup = OSError("Retirement request failed before returning a control")
        owner = host()
        original_retire = owner.retire

        try:
            with patch.object(target=owner, attribute="retire", side_effect=cleanup):
                with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
                    async with owner:
                        raise original

            self.assertEqual(first=(original, cleanup), second=raised.exception.exceptions)
        finally:
            await settle_cleanup(control=original_retire())

    async def test_body_and_native_retirement_failures_preserve_original_causes(self):
        original = OSError("Owner work failed")
        cleanup = RuntimeError("Native resource closure failed")

        def close():
            raise cleanup

        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            async with host() as owner:
                owner.adopt(contribution=OwnedScience(close=close), requirements=frozenset())
                raise original

        (body_failure, retirement_failure) = raised.exception.exceptions

        self.assertIs(expr1=original, expr2=body_failure)
        observation = await owner.retire().observe(after=None, wait_seconds=0)

        self.assertIs(expr1=retirement_failure, expr2=observation.failures[0].cause)
        self.assertEqual(first=(cleanup,), second=retirement_failure.exceptions)

    async def test_repeated_cancellation_waits_for_native_resource_close(self):
        entered = Event()
        release_cleanup = Event()
        exited = asyncio.Event()
        closed = []
        owner = host()

        def close():
            entered.set()

            if not release_cleanup.wait(timeout=(release_timeout_seconds := 5)):
                raise TimeoutError(f"{release_cleanup.is_set()=} after waiting {release_timeout_seconds=}")

            closed.append(True)

        async def use_host():
            try:
                async with owner:
                    owner.adopt(contribution=OwnedScience(close=close), requirements=frozenset())
            finally:
                exited.set()

        task = asyncio.create_task(coro=use_host())

        try:
            async with asyncio.timeout(10):
                self.assertTrue(expr=await asyncio.to_thread(entered.wait, timeout=5))
                task.cancel("first exit interruption")
                loop = asyncio.get_running_loop()

                loop.call_soon(task.cancel, "second exit interruption")
                cancellations_delivered = asyncio.Event()

                loop.call_soon(cancellations_delivered.set)
                await cancellations_delivered.wait()
                self.assertFalse(expr=task.done())
                self.assertFalse(expr=exited.is_set())
                release_cleanup.set()

                with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
                    await task

            self.assertEqual(first=2, second=len(raised.exception.exceptions))
            self.assertTrue(expr=all(isinstance(cause, asyncio.CancelledError) for cause in raised.exception.exceptions))
            self.assertEqual(first=[True], second=closed)
            observation = await owner.retire().observe(after=None, wait_seconds=0)

            self.assertTrue(expr=observation.settled)
        finally:
            release_cleanup.set()
            await asyncio.gather(task, return_exceptions=True)
