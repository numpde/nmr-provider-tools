"""Require recovery of one allocation without restarting healthy provider work."""

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
