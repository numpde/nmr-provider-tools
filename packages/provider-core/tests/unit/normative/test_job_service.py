"""A provider service preserves Attempt ordering and selected results through stop."""

import asyncio
import json
import unittest

from provider_core.analysis import AnalysisOutcomeUnresolved
from provider_core.api_errors import ApiFailure
from provider_core.transport import ExchangeFailed
from provider_core.job_service import JobOffering, JobService, JobServicePolicy, HostFailurePolicy
from provider_core.ownership import LiveAttemptLimits
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering
from tests.fakes.api import Transport, api, reply as api_reply
from tests.fakes.checkpoint_execution import RecordingDelivery, CausalDelivery
from tests.fakes.controller import lifecycle_replies
from tests.fakes.hello import receipt
from tests.fakes.job_feed import page
from tests.fakes.live_analysis import LiveSession


class ResidentWorker:
    async def observe_exit(self, *, wait_seconds):
        await asyncio.sleep(wait_seconds)
        raise TimeoutError()


class JobServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, session, delivery, stopping):
        jobs_page = page()
        jobs_page['next_cursor'] = None
        discovery = Transport(api_reply(json.dumps(jobs_page).encode()))
        hello = Transport(api_reply(receipt()), api_reply(receipt()))
        attempt = Transport(*lifecycle_replies())
        transports = iter((discovery, hello, attempt))

        service = JobService(
            provider_ref='provider:example',
            offerings=(
                JobOffering(
                    announcement=AnalysisOffering('nmr_predict', 'Test science'),
                    host=ResidentWorker(),
                    consumer_id='test-provider',
                    generation='one',
                    new_session=lambda: session,
                ),
            ),
            host_failure_policy=HostFailurePolicy.STOP,
            limits=LiveAttemptLimits(1, 262144, 131072),
            display_name='Test provider',
            description='A synthetic API provider',
            policy=JobServicePolicy(
                feed_seconds=0.01,
                hello_seconds=10,
                observation_seconds=1,
                recovery_seconds=90,
                retry_max_seconds=0.02,
                settle_seconds=1,
            ),
            new_api=lambda: api(next(transports), 'https://api.example'),
            new_delivery=lambda: delivery,
            stopping=stopping,
        )

        return (service, hello)

    async def test_job_reaches_delivery_and_withdraws_on_stop(self):
        stopping = asyncio.Event()
        session = LiveSession()

        class StopAfterDelivery(RecordingDelivery):
            async def submit(self, delivery_item):
                accepted = await super().submit(delivery_item)

                stopping.set()

                return accepted

        delivery = StopAfterDelivery()
        (service, hello) = self.service(session=session, delivery=delivery, stopping=stopping)

        await asyncio.wait_for(service.serve(), timeout=3)
        self.assertEqual(['prepare', 'execute', 'retire'], session.events)
        self.assertEqual(1, len(delivery.delivery_outcomes))
        self.assertEqual((), service.claims.live_owners())
        self.assertIsNone(service.active)
        self.assertFalse(service.publisher.withdrawal_required)
        self.assertEqual(2, len(hello.calls))

    async def test_stop_while_delivery_is_full_keeps_the_original_result(self):
        stopping = asyncio.Event()
        session = LiveSession()
        delivery = CausalDelivery()
        (service, _) = self.service(session=session, delivery=delivery, stopping=stopping)
        task = asyncio.create_task(service.serve())

        try:
            await asyncio.wait_for(delivery.wait_for_offers(1), timeout=2)
            original = delivery.offers[0].body

            stopping.set()
            delivery.answer('full')
            await asyncio.wait_for(delivery.wait_for_offers(2), timeout=2)
            self.assertFalse(task.done())
            self.assertEqual(1, session.events.count('execute'))
            self.assertEqual(original, delivery.offers[1].body)
            delivery.answer('accepted')
            await asyncio.wait_for(task, timeout=3)
            self.assertEqual((), service.claims.live_owners())
        finally:
            delivery.release_for_teardown()
            stopping.set()

            if not task.done():
                await asyncio.wait_for(task, timeout=3)

    async def test_unknown_scientific_failure_never_becomes_a_public_fail(self):
        stopping = asyncio.Event()

        class UnresolvedSession(LiveSession):
            async def execute(self, prepared, *, report_condition):
                self.events.append('execute')
                raise AnalysisOutcomeUnresolved('Scientific connection was lost')

        session = UnresolvedSession()
        delivery = RecordingDelivery()
        (service, _) = self.service(session=session, delivery=delivery, stopping=stopping)

        with self.assertRaises(ExceptionGroup):
            await asyncio.wait_for(service.serve(), timeout=3)

        self.assertEqual(1, session.events.count('execute'))
        self.assertEqual([], delivery.delivery_outcomes)
        self.assertTrue(service.active.shutdown_ready)
        self.assertFalse(service.publisher.withdrawal_required)

    async def test_preexisting_stop_does_not_announce_or_start_work(self):
        stopping = asyncio.Event()

        stopping.set()
        session = LiveSession()
        (service, hello) = self.service(session=session, delivery=RecordingDelivery(), stopping=stopping)

        await service.serve()

        self.assertEqual([], session.events)
        self.assertEqual([], hello.calls)

    async def test_lost_initial_hello_reply_still_withdraws_availability(self):
        stopping = asyncio.Event()
        session = LiveSession()
        (service, hello) = self.service(session=session, delivery=RecordingDelivery(), stopping=stopping)
        hello.replies = iter((ExchangeFailed("Hello reply was lost"), api_reply(receipt())))

        with self.assertRaises(ApiFailure):
            await service.serve()

        self.assertEqual([], session.events)
        self.assertEqual(2, len(hello.calls))
        self.assertEqual([], json.loads(hello.calls[1][0].content)["analysis_offerings"])
        self.assertFalse(service.publisher.withdrawal_required)

    async def test_worker_exit_withdraws_availability(self):
        stopping = asyncio.Event()
        session = LiveSession()
        (service, hello) = self.service(session=session, delivery=RecordingDelivery(), stopping=stopping)

        async def exited(*, wait_seconds):
            return "worker exited"

        async def wait_for_stop():
            await stopping.wait()

        service.offerings[0].host.observe_exit = exited
        service.jobs = wait_for_stop

        with self.assertRaises(RuntimeError):
            await service.serve()

        self.assertEqual([], session.events)
        self.assertEqual([], hello.calls)
        self.assertFalse(service.publisher.withdrawal_required)
