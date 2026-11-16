"""Require recovery of one allocation without restarting healthy provider work."""

import asyncio
from threading import Event
import unittest

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import CapacityUnavailable, NotAdmitted, OperationPolicy, settle_cleanup
from provider_core.submission import Submission

from .test_eager_initialization import LoadingContribution


class LiveReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = await self.enterAsyncContext(InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        ))
        self.policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=4096,
            reject_encoded_result_over_bytes=4096,
        )
        self.healthy = LoadingContribution()
        self.healthy.release.set()
        self.failed = LoadingContribution()
        self.failed.failure = OSError("Model could not load")
        self.failed.release.set()
        self.healthy_offering = self.host.adopt(contribution=self.healthy, requirements=frozenset())
        self.failed_offering = self.host.adopt(contribution=self.failed, requirements=frozenset())

        with self.assertRaises(expected_exception=OSError) as raised:
            await self.host.initialize(allocation=self.failed_offering.allocation)

        self.assertIs(expr1=self.failed.failure, expr2=raised.exception)

    async def calculate(self, *, analysis, offering):
        submission = Submission.from_call(
            analysis_executor=analysis,
            call_binding=offering.call(method=offering.contribution.calculate),
            states={},
            reservation_ref=analysis.reservation(allocation=offering.allocation),
            operation_policy=self.policy,
        )
        admission = submission.submit()

        self.assertNotIsInstance(obj=admission, cls=NotAdmitted)
        observation = await submission.wait()
        observation.require_result()

        await settle_cleanup(control=submission.operation.release_result(result=observation.result))

    async def test_replace_failed_model_after_healthy_request_completed(self):
        healthy_analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await self.calculate(analysis=healthy_analysis, offering=self.healthy_offering)
        await settle_cleanup(control=healthy_analysis.close())
        await settle_cleanup(control=self.host.retire_allocation(self.failed_offering.allocation))
        self.assertTrue(expr=self.failed.closed)

        replacement = LoadingContribution()
        replacement.release.set()
        replacement_offering = self.host.adopt(contribution=replacement, requirements=frozenset())
        await self.host.initialize(allocation=replacement_offering.allocation)

        replacement_analysis = self.host.open_analysis(allocations=frozenset({replacement_offering.allocation}))
        await self.calculate(analysis=replacement_analysis, offering=replacement_offering)
        await settle_cleanup(control=replacement_analysis.close())

        healthy_analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await self.calculate(analysis=healthy_analysis, offering=self.healthy_offering)
        await settle_cleanup(control=healthy_analysis.close())

        with self.assertRaises(expected_exception=CapacityUnavailable):
            self.host.open_analysis(allocations=frozenset({self.failed_offering.allocation}))

        self.assertEqual(first=1, second=self.healthy.loads)
        self.assertEqual(first=1, second=replacement.loads)

    async def test_replace_failed_model_while_healthy_request_remains_open(self):
        healthy_analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await self.calculate(analysis=healthy_analysis, offering=self.healthy_offering)
        await settle_cleanup(control=self.host.retire_allocation(self.failed_offering.allocation))

        replacement = LoadingContribution()
        replacement.release.set()
        replacement_offering = self.host.adopt(contribution=replacement, requirements=frozenset())
        await self.host.initialize(allocation=replacement_offering.allocation)

        replacement_analysis = self.host.open_analysis(allocations=frozenset({replacement_offering.allocation}))
        await self.calculate(analysis=replacement_analysis, offering=replacement_offering)
        await self.calculate(analysis=healthy_analysis, offering=self.healthy_offering)
        await settle_cleanup(control=replacement_analysis.close())
        await settle_cleanup(control=healthy_analysis.close())

        self.assertEqual(first=1, second=self.healthy.loads)
        self.assertEqual(first=1, second=replacement.loads)

    async def test_successful_retirements_do_not_accumulate_in_host_inventory(self):
        healthy_analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await settle_cleanup(control=healthy_analysis.close())
        await settle_cleanup(control=self.host.retire_allocation(self.failed_offering.allocation))
        retired_allocations = {self.failed_offering.allocation}

        for _ in range(5):
            self.assertEqual(first=frozenset({self.healthy_offering.allocation}), second=self.host.allocations)
            replacement = LoadingContribution()
            replacement.release.set()
            offering = self.host.adopt(contribution=replacement, requirements=frozenset())
            self.assertNotIn(member=offering.allocation, container=retired_allocations)
            await self.host.initialize(allocation=offering.allocation)
            await settle_cleanup(control=self.host.retire_allocation(offering.allocation))
            retired_allocations.add(offering.allocation)

        self.assertEqual(first=frozenset({self.healthy_offering.allocation}), second=self.host.allocations)

        for retired_allocation in retired_allocations:
            with self.assertRaises(expected_exception=(CapacityUnavailable, ValueError)):
                self.host.open_analysis(allocations=frozenset({retired_allocation}))

    async def test_host_shutdown_owns_new_allocations_and_refuses_later_adoption(self):
        analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await self.calculate(analysis=analysis, offering=self.healthy_offering)
        await settle_cleanup(control=analysis.close())

        added = LoadingContribution()
        offering = self.host.adopt(contribution=added, requirements=frozenset())
        initialization = asyncio.create_task(coro=self.host.initialize(allocation=offering.allocation))

        try:
            self.assertTrue(expr=await asyncio.to_thread(added.entered.wait, timeout=2))
            retirement = self.host.retire()

            with self.assertRaises(expected_exception=CapacityUnavailable):
                self.host.adopt(contribution=LoadingContribution(), requirements=frozenset())

            await asyncio.sleep(delay=0)
            self.assertFalse(expr=retirement.task.done())
            self.assertFalse(expr=added.closed)
            added.release.set()
            await initialization
            await settle_cleanup(control=retirement)
            self.assertTrue(expr=added.closed)
        finally:
            added.release.set()
            await asyncio.gather(initialization, return_exceptions=True)

    async def test_pending_retirement_remains_owned_until_cleanup_finishes(self):
        entered = Event()
        release = Event()

        class HeldClose(LoadingContribution):
            def close(self) -> None:
                entered.set()

                if not release.wait(timeout=5):
                    raise TimeoutError("Test did not release model cleanup")

                super().close()

        analysis = self.host.open_analysis(allocations=frozenset({self.healthy_offering.allocation}))
        await settle_cleanup(control=analysis.close())
        original = HeldClose()
        offering = self.host.adopt(contribution=original, requirements=frozenset())
        retirement = self.host.retire_allocation(offering.allocation)

        try:
            self.assertTrue(expr=await asyncio.to_thread(entered.wait, timeout=2))
            self.assertIn(member=offering.allocation, container=self.host.allocations)
            self.assertFalse(expr=retirement.task.done())
            self.assertIs(expr1=retirement, expr2=self.host.retire_allocation(offering.allocation))
            release.set()
            await settle_cleanup(control=retirement)
            await settle_cleanup(control=retirement)
            self.assertNotIn(member=offering.allocation, container=self.host.allocations)
            self.assertTrue(expr=original.closed)
        finally:
            release.set()

    async def test_failed_cleanup_remains_owned_and_surfaces_original_error(self):
        original_failure = OSError("Cannot release model resources")

        class FailedClose(LoadingContribution):
            def close(self) -> None:
                raise original_failure

        host = InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        try:
            healthy = host.adopt(contribution=LoadingContribution(), requirements=frozenset())
            analysis = host.open_analysis(allocations=frozenset({healthy.allocation}))
            await settle_cleanup(control=analysis.close())
            failed = host.adopt(contribution=FailedClose(), requirements=frozenset())
            retirement = host.retire_allocation(failed.allocation)

            with self.assertRaises(expected_exception=OSError) as raised:
                await settle_cleanup(control=retirement)

            self.assertIs(expr1=original_failure, expr2=raised.exception)
            self.assertIn(member=failed.allocation, container=host.allocations)
            self.assertIs(expr1=retirement, expr2=host.retire_allocation(failed.allocation))

            with self.assertRaises(expected_exception=CapacityUnavailable):
                host.open_analysis(allocations=frozenset({failed.allocation}))
        finally:
            shutdown = host.retire()
            await shutdown.task

        self.assertIs(expr1=original_failure, expr2=shutdown.failures[0].cause.exceptions[0])
