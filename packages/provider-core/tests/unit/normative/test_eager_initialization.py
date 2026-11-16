"""Exercise startup ownership when loading succeeds, fails, or outlives a waiter."""

import asyncio
from dataclasses import dataclass
from threading import Event
import unittest

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import CapacityUnavailable
from provider_core.scientific import AssetAccess, Contribution, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Result:
    count: int


class LoadingContribution(Contribution):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.loads = 0
        self.closed = False
        self.failure = None

    def initialize(self, *, assets: AssetAccess) -> None:
        self.loads += 1
        self.entered.set()

        if not self.release.wait(timeout=5):
            raise TimeoutError("Test did not release loading")

        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed = True

    @operation()
    def calculate(self) -> Result:
        return Result(count=1)


class EagerInitializationTests(unittest.IsolatedAsyncioTestCase):
    def host(self, *, seconds):
        return InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=seconds,
            retirement_seconds=5,
        )

    async def test_cancelled_waiter_joins_original_load(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first_waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                first_waiter.cancel()

                with self.assertRaises(asyncio.CancelledError):
                    await first_waiter

                with self.assertRaises(CapacityUnavailable):
                    host.open_analysis(allocations=frozenset({offering.allocation}))

                contribution.release.set()
                await host.initialize(allocation=offering.allocation)
                self.assertEqual(first=1, second=contribution.loads)
            finally:
                contribution.release.set()

        self.assertTrue(expr=contribution.closed)

    async def test_timeout_does_not_repeat_load_and_shutdown_waits(self):
        async with self.host(seconds=0.05) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())

            try:
                with self.assertRaises(TimeoutError):
                    await host.initialize(allocation=offering.allocation)

                with self.assertRaises(TimeoutError):
                    await host.initialize(allocation=offering.allocation)

                self.assertEqual(first=1, second=contribution.loads)
                self.assertFalse(expr=contribution.closed)
                retirement = host.retire()
                await asyncio.sleep(delay=0)
                self.assertFalse(expr=retirement.task.done())
            finally:
                contribution.release.set()

        self.assertTrue(expr=contribution.closed)

    async def test_original_loading_failure_is_not_retried(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = OSError("Model loading failed")
            contribution.release.set()
            offering = host.adopt(contribution=contribution, requirements=frozenset())

            for _ in range(2):
                with self.assertRaises(OSError) as raised:
                    await host.initialize(allocation=offering.allocation)

                self.assertIs(expr1=contribution.failure, expr2=raised.exception)

            self.assertEqual(first=1, second=contribution.loads)

        self.assertTrue(expr=contribution.closed)

    async def test_late_loading_failure_survives_cancelled_waiter(self):
        host = self.host(seconds=5)
        contribution = LoadingContribution()
        contribution.failure = OSError("Late model loading failure")
        offering = host.adopt(contribution=contribution, requirements=frozenset())
        waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

        try:
            self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
            waiter.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await waiter

            contribution.release.set()
            retirement = host.retire_allocation(offering.allocation)
            await retirement.task

            self.assertIs(expr1=contribution.failure, expr2=retirement.failures[0].cause)
            self.assertTrue(expr=contribution.closed)
        finally:
            contribution.release.set()

        await host.retire().task
