"""Exercise startup ownership when loading succeeds, fails, or outlives a waiter."""

import asyncio
from dataclasses import dataclass
from threading import Event
import unittest
from unittest.mock import patch

from provider_core import embedded_host
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

    async def test_success_is_reused_without_a_second_load(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.release.set()
            offering = host.adopt(contribution=contribution, requirements=frozenset())

            await host.initialize(allocation=offering.allocation)
            await host.initialize(allocation=offering.allocation)

            self.assertEqual(first=1, second=contribution.loads)

    async def test_concurrent_waiters_share_one_load(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))
            second = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                contribution.release.set()
                await asyncio.gather(first, second)
                self.assertEqual(first=1, second=contribution.loads)
            finally:
                contribution.release.set()

    async def test_cancelling_one_waiter_does_not_cancel_another(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))
            second = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                first.cancel()

                with self.assertRaises(expected_exception=asyncio.CancelledError):
                    await first

                self.assertFalse(expr=second.done())
                contribution.release.set()
                await second
                self.assertEqual(first=1, second=contribution.loads)
            finally:
                contribution.release.set()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_concurrent_waiters_receive_the_original_loading_failure(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = OSError("Original model failure")
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))
            second = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                contribution.release.set()
                failures = await asyncio.gather(first, second, return_exceptions=True)

                self.assertIs(expr1=contribution.failure, expr2=failures[0])
                self.assertIs(expr1=contribution.failure, expr2=failures[1])
                self.assertEqual(first=1, second=contribution.loads)
            finally:
                contribution.release.set()

        self.assertTrue(expr=contribution.closed)

    async def test_blocked_loading_does_not_block_an_independent_allocation(self):
        async with self.host(seconds=5) as host:
            blocked = LoadingContribution()
            healthy = LoadingContribution()
            healthy.release.set()
            blocked_offering = host.adopt(contribution=blocked, requirements=frozenset())
            healthy_offering = host.adopt(contribution=healthy, requirements=frozenset())
            blocked_waiter = asyncio.create_task(coro=host.initialize(allocation=blocked_offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(blocked.entered.wait, timeout=2))
                await host.initialize(allocation=healthy_offering.allocation)
                self.assertFalse(expr=blocked_waiter.done())
                analysis = host.open_analysis(allocations=frozenset({healthy_offering.allocation}))
                await analysis.close().task
            finally:
                blocked.release.set()
                await blocked_waiter

        self.assertTrue(expr=blocked.closed)
        self.assertTrue(expr=healthy.closed)

    async def test_retirement_requests_join_the_same_pending_cleanup(self):
        host = self.host(seconds=5)
        contribution = LoadingContribution()
        offering = host.adopt(contribution=contribution, requirements=frozenset())
        waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

        try:
            self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
            first = host.retire_allocation(offering.allocation)
            second = host.retire_allocation(offering.allocation)
            self.assertIs(expr1=first, expr2=second)
            await asyncio.sleep(delay=0)
            self.assertFalse(expr=first.task.done())
            self.assertFalse(expr=contribution.closed)
            contribution.release.set()
            await waiter
            await first.task
            self.assertTrue(expr=contribution.closed)
            self.assertTrue(expr=first.settled)
        finally:
            contribution.release.set()
            await host.retire().task

    async def test_model_timeout_before_host_deadline_is_original_error(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = TimeoutError("Model service timed out")
            contribution.release.set()
            offering = host.adopt(contribution=contribution, requirements=frozenset())

            with self.assertRaises(expected_exception=TimeoutError) as raised:
                await host.initialize(allocation=offering.allocation)

            self.assertIs(expr1=contribution.failure, expr2=raised.exception)

        self.assertTrue(expr=contribution.closed)

    async def test_cancellation_and_cleanup_failure_both_reach_the_caller(self):
        cancellation = asyncio.CancelledError("Caller stopped startup")
        cleanup_failure = OSError("Model cleanup failed")

        class FailingClose(LoadingContribution):
            def close(self) -> None:
                raise cleanup_failure

        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            async with self.host(seconds=5) as host:
                host.adopt(contribution=FailingClose(), requirements=frozenset())
                raise cancellation

        def contained(*, failure):
            if isinstance(failure, BaseExceptionGroup):
                return any(contained(failure=cause) for cause in failure.exceptions)

            return failure is cleanup_failure

        self.assertIn(member=cancellation, container=raised.exception.exceptions)
        self.assertTrue(expr=contained(failure=raised.exception))

    async def test_one_cleanup_failure_does_not_stop_other_allocations_retiring(self):
        cleanup_failure = OSError("One model failed to close")

        class FailingClose(LoadingContribution):
            def close(self) -> None:
                raise cleanup_failure

        host = self.host(seconds=5)
        healthy = LoadingContribution()
        failed = FailingClose()
        host.adopt(contribution=failed, requirements=frozenset())
        host.adopt(contribution=healthy, requirements=frozenset())
        retirement = host.retire()
        await retirement.task

        self.assertTrue(expr=healthy.closed)
        self.assertFalse(expr=retirement.settled)
        self.assertIs(expr1=cleanup_failure, expr2=retirement.failures[0].cause.exceptions[0])

    async def test_cancelled_waiter_does_not_repackage_another_waiters_failure(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = OSError("Original loading failure")
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))
            second = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                first.cancel()

                with self.assertRaises(expected_exception=asyncio.CancelledError):
                    await first

                contribution.release.set()

                with self.assertRaises(expected_exception=OSError) as raised:
                    await second

                self.assertIs(expr1=contribution.failure, expr2=raised.exception)
            finally:
                contribution.release.set()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_cancellation_after_another_waiter_observes_failure_does_not_report_it_twice(self):
        original_wait = embedded_host.await_original
        delayed = asyncio.Event()
        hold = asyncio.Event()
        wait_count = 0

        async def delay_second_waiter(original, *, deadline):
            nonlocal wait_count
            wait_count += 1
            this_wait = wait_count

            try:
                return await original_wait(original=original, deadline=deadline)
            except OSError:
                if this_wait == 2:
                    delayed.set()
                    await hold.wait()

                raise

        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = OSError("Original loading failure")
            offering = host.adopt(contribution=contribution, requirements=frozenset())

            with patch.object(target=embedded_host, attribute="await_original", new=delay_second_waiter):
                first = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))
                second = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

                try:
                    self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                    contribution.release.set()

                    with self.assertRaises(expected_exception=OSError) as raised:
                        await first

                    self.assertIs(expr1=contribution.failure, expr2=raised.exception)
                    await asyncio.wait_for(fut=delayed.wait(), timeout=2)
                    second.cancel()

                    with self.assertRaises(expected_exception=asyncio.CancelledError):
                        await second
                finally:
                    contribution.release.set()
                    hold.set()
                    await asyncio.gather(first, second, return_exceptions=True)

        self.assertTrue(expr=contribution.closed)

    async def test_foreign_allocation_is_rejected(self):
        async with self.host(seconds=5) as first_host:
            async with self.host(seconds=5) as second_host:
                contribution = LoadingContribution()
                offering = first_host.adopt(contribution=contribution, requirements=frozenset())

                with self.assertRaises(expected_exception=ValueError):
                    await second_host.initialize(allocation=offering.allocation)

                self.assertEqual(first=0, second=contribution.loads)

    async def test_retiring_host_refuses_initialization(self):
        host = self.host(seconds=5)
        contribution = LoadingContribution()
        offering = host.adopt(contribution=contribution, requirements=frozenset())
        retirement = host.retire()

        with self.assertRaises(expected_exception=CapacityUnavailable):
            await host.initialize(allocation=offering.allocation)

        await retirement.task
        self.assertEqual(first=0, second=contribution.loads)

    async def test_active_analysis_refuses_initialization(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            analysis = host.open_analysis(allocations=frozenset({offering.allocation}))

            with self.assertRaises(expected_exception=CapacityUnavailable):
                await host.initialize(allocation=offering.allocation)

            await analysis.close().task
            self.assertEqual(first=0, second=contribution.loads)

    async def test_cancelled_waiter_joins_original_load(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            first_waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                first_waiter.cancel()

                with self.assertRaises(expected_exception=asyncio.CancelledError):
                    await first_waiter

                with self.assertRaises(expected_exception=CapacityUnavailable):
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
                with self.assertRaises(expected_exception=TimeoutError):
                    await host.initialize(allocation=offering.allocation)

                with self.assertRaises(expected_exception=TimeoutError):
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
                with self.assertRaises(expected_exception=OSError) as raised:
                    await host.initialize(allocation=offering.allocation)

                self.assertIs(expr1=contribution.failure, expr2=raised.exception)

            self.assertEqual(first=1, second=contribution.loads)

        self.assertTrue(expr=contribution.closed)

    async def test_failed_allocation_does_not_prevent_an_independent_one(self):
        async with self.host(seconds=5) as host:
            failed = LoadingContribution()
            failed.failure = OSError("Model loading failed")
            failed.release.set()
            healthy = LoadingContribution()
            healthy.release.set()
            failed_offering = host.adopt(contribution=failed, requirements=frozenset())
            healthy_offering = host.adopt(contribution=healthy, requirements=frozenset())

            with self.assertRaises(expected_exception=OSError) as raised:
                await host.initialize(allocation=failed_offering.allocation)

            self.assertIs(expr1=failed.failure, expr2=raised.exception)
            await host.initialize(allocation=healthy_offering.allocation)
            analysis = host.open_analysis(allocations=frozenset({healthy_offering.allocation}))
            await analysis.close().task

            self.assertEqual(first=1, second=healthy.loads)

    async def test_fresh_contribution_can_load_after_failed_one_is_retired(self):
        async with self.host(seconds=5) as host:
            failed = LoadingContribution()
            failed.failure = OSError("Model loading failed")
            failed.release.set()
            failed_offering = host.adopt(contribution=failed, requirements=frozenset())

            with self.assertRaises(expected_exception=OSError):
                await host.initialize(allocation=failed_offering.allocation)

            await host.retire_allocation(failed_offering.allocation).task
            replacement = LoadingContribution()
            replacement.release.set()
            replacement_offering = host.adopt(contribution=replacement, requirements=frozenset())
            await host.initialize(allocation=replacement_offering.allocation)

            self.assertTrue(expr=failed.closed)
            self.assertEqual(first=1, second=replacement.loads)

    async def test_late_loading_failure_survives_cancelled_waiter(self):
        host = self.host(seconds=5)
        contribution = LoadingContribution()
        contribution.failure = OSError("Late model loading failure")
        offering = host.adopt(contribution=contribution, requirements=frozenset())
        waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

        try:
            self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
            waiter.cancel()

            with self.assertRaises(expected_exception=asyncio.CancelledError):
                await waiter

            contribution.release.set()
            retirement = host.retire_allocation(offering.allocation)
            await retirement.task

            self.assertIs(expr1=contribution.failure, expr2=retirement.failures[0].cause)
            self.assertTrue(expr=contribution.closed)
        finally:
            contribution.release.set()

        await host.retire().task

    async def test_observed_late_failure_is_not_reported_again_as_cleanup(self):
        async with self.host(seconds=5) as host:
            contribution = LoadingContribution()
            contribution.failure = OSError("Late model loading failure")
            offering = host.adopt(contribution=contribution, requirements=frozenset())
            waiter = asyncio.create_task(coro=host.initialize(allocation=offering.allocation))

            try:
                self.assertTrue(expr=await asyncio.to_thread(contribution.entered.wait, timeout=2))
                waiter.cancel()

                with self.assertRaises(expected_exception=asyncio.CancelledError):
                    await waiter

                contribution.release.set()

                with self.assertRaises(expected_exception=OSError) as raised:
                    await host.initialize(allocation=offering.allocation)

                self.assertIs(expr1=contribution.failure, expr2=raised.exception)
            finally:
                contribution.release.set()

        self.assertTrue(expr=contribution.closed)

    async def test_late_failure_after_timeout_is_reported_on_retirement(self):
        host = self.host(seconds=0.05)
        contribution = LoadingContribution()
        contribution.failure = OSError("Late model loading failure")
        offering = host.adopt(contribution=contribution, requirements=frozenset())

        try:
            with self.assertRaises(expected_exception=TimeoutError):
                await host.initialize(allocation=offering.allocation)

            contribution.release.set()
            retirement = host.retire_allocation(offering.allocation)
            await retirement.task

            self.assertIs(expr1=contribution.failure, expr2=retirement.failures[0].cause)
            self.assertTrue(expr=contribution.closed)
        finally:
            contribution.release.set()

        await host.retire().task

    async def test_late_model_timeout_is_not_confused_with_the_host_deadline(self):
        host = self.host(seconds=0.05)
        contribution = LoadingContribution()
        contribution.failure = TimeoutError("Model service timed out")
        offering = host.adopt(contribution=contribution, requirements=frozenset())

        try:
            with self.assertRaises(expected_exception=TimeoutError):
                await host.initialize(allocation=offering.allocation)

            contribution.release.set()
            retirement = host.retire_allocation(offering.allocation)
            await retirement.task

            self.assertIs(expr1=contribution.failure, expr2=retirement.failures[0].cause)
        finally:
            contribution.release.set()

        await host.retire().task

    async def test_completed_model_failure_is_not_lost_when_waiter_times_out(self):
        original_wait = embedded_host.await_original
        host_timeout = TimeoutError("Startup wait expired")

        async def wait_races_with_failure(original, *, deadline):
            try:
                return await original_wait(original=original, deadline=deadline)
            except OSError:
                raise host_timeout from None

        host = self.host(seconds=5)
        contribution = LoadingContribution()
        contribution.failure = OSError("Model loading failed at the deadline")
        contribution.release.set()
        offering = host.adopt(contribution=contribution, requirements=frozenset())

        with patch.object(target=embedded_host, attribute="await_original", new=wait_races_with_failure):
            with self.assertRaises(expected_exception=TimeoutError) as raised:
                await host.initialize(allocation=offering.allocation)

        self.assertIs(expr1=host_timeout, expr2=raised.exception)
        retirement = host.retire_allocation(offering.allocation)
        await retirement.task

        self.assertIs(expr1=contribution.failure, expr2=retirement.failures[0].cause)
        self.assertTrue(expr=contribution.closed)
        await host.retire().task

    async def test_late_loading_and_cleanup_failures_are_both_retained(self):
        cleanup_failure = OSError("Model cleanup failed")

        class FailingClose(LoadingContribution):
            def close(self) -> None:
                raise cleanup_failure

        host = self.host(seconds=0.05)
        contribution = FailingClose()
        contribution.failure = OSError("Late model loading failure")
        offering = host.adopt(contribution=contribution, requirements=frozenset())

        try:
            with self.assertRaises(expected_exception=TimeoutError):
                await host.initialize(allocation=offering.allocation)

            contribution.release.set()
            retirement = host.retire_allocation(offering.allocation)
            await retirement.task
            failures = retirement.failures[0].cause.exceptions

            self.assertIn(member=contribution.failure, container=failures)
            self.assertIn(member=cleanup_failure, container=failures)
        finally:
            contribution.release.set()

        await host.retire().task
