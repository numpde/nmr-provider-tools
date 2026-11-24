"""A provider service preserves Attempt ordering and selected results through stop."""

import asyncio
import json
from itertools import chain, repeat
import unittest

from provider_core.analysis import AnalysisOutcomeUnresolved
from provider_core.chat import ChatFailed
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
from tests.fakes.job_service import JobApi
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
        # A slower Attempt may allow another poll; the feed then has no new jobs.
        empty_page = {**jobs_page, "jobs": []}
        discovery.replies = chain(discovery.replies, repeat(api_reply(json.dumps(empty_page).encode())))
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

    def test_shutdown_report_preserves_redacted_original_failure_graph(self):
        (service, _) = self.service(session=LiveSession(), delivery=RecordingDelivery(), stopping=asyncio.Event())
        service.diagnostic_secrets = ("unlabelled-private-key",)
        try:
            raise ChatFailed("http_status", "Endpoint refused unlabelled-private-key")
        except ChatFailed as cause:
            failure = RuntimeError("Attempt shutdown incomplete")
            failure.__cause__ = cause

        with self.assertLogs("provider_core.job_service", level="ERROR") as captured:
            service.report_failure(failure)

        output = captured.output[0]
        self.assertIn("ChatFailed", output)
        self.assertIn("Endpoint refused", output)
        self.assertIn("relationships", output)
        self.assertIn("[redacted]", output)
        self.assertNotIn("unlabelled-private-key", output)

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
        self.assertIsNone(service.admitting)
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
        self.assertTrue(all(owner.shutdown_ready for owner in service.claims.live_owners()))
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


class ConcurrentJobServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, limits, preparations):
        fixture = JobApi()
        fixture.jobs = {f"job:{index}": ("nmr_predict", b"same input") for index in range(3)}
        stopping = asyncio.Event()
        sessions = []
        deliveries = []
        entered = asyncio.Queue()
        gates = [(asyncio.Event(), asyncio.Event()) for _ in range(3)]

        class Session(LiveSession):
            async def prepare(self, *args, **kwargs):
                entered.put_nowait(("prepare", self.index))
                await gates[self.index][0].wait()

                return await super().prepare(*args, **kwargs)

            async def execute(self, *args, **kwargs):
                entered.put_nowait(("execute", self.index))
                await gates[self.index][1].wait()

                return await super().execute(*args, **kwargs)

        def new_session():
            session = Session()
            session.index = len(sessions)

            sessions.append(session)

            return session

        def new_delivery():
            delivery = CausalDelivery()

            deliveries.append(delivery)

            return delivery

        service = JobService(
            provider_ref="provider:example",
            offerings=(
                JobOffering(
                    announcement=AnalysisOffering("nmr_predict", "Test science"),
                    host=ResidentWorker(),
                    consumer_id="test",
                    generation="one",
                    new_session=new_session,
                ),
            ),
            host_failure_policy=HostFailurePolicy.STOP,
            limits=limits,
            maximum_preparations=preparations,
            display_name="Test",
            description="Concurrent service fixture",
            policy=JobServicePolicy(0.01, 10, 1, 90, 0.02, 1),
            new_api=fixture.new_api,
            new_delivery=new_delivery,
            stopping=stopping,
        )

        return (service, fixture, sessions, deliveries, entered, gates)

    async def cleanup(self, service, task, deliveries, gates):
        service.stopping.set()

        for (prepare, execute) in gates:
            prepare.set()
            execute.set()

        for delivery in deliveries:
            delivery.release_for_teardown()

        await asyncio.wait_for(task, timeout=3)

    async def test_progress_during_science_and_delivery_waits(self):
        (service, fixture, sessions, deliveries, entered, gates) = self.service(
            limits=LiveAttemptLimits(3, 393216, 393216, 30),
            preparations=2,
        )

        task = asyncio.create_task(service.serve())

        self.addAsyncCleanup(self.cleanup, service, task, deliveries, gates)
        self.assertEqual(("prepare", 0), await asyncio.wait_for(entered.get(), 2))
        self.assertEqual(("prepare", 1), await asyncio.wait_for(entered.get(), 2))
        self.assertEqual(3, len(service.claims.live_owners()))
        gates[0][0].set()
        events = {await asyncio.wait_for(entered.get(), 2), await asyncio.wait_for(entered.get(), 2)}

        self.assertEqual({("execute", 0), ("prepare", 2)}, events)
        gates[1][0].set()
        self.assertEqual(("execute", 1), await asyncio.wait_for(entered.get(), 2))

        # Job 1 reaches delivery while Job 0 still owns blocked science.
        gates[1][1].set()

        await asyncio.wait_for(deliveries[1].wait_for_offers(1), 2)
        self.assertFalse(gates[0][1].is_set())
        gates[0][1].set()
        await asyncio.wait_for(deliveries[0].wait_for_offers(1), 2)
        gates[2][0].set()
        self.assertEqual(("execute", 2), await asyncio.wait_for(entered.get(), 2))
        gates[2][1].set()
        await asyncio.wait_for(deliveries[2].wait_for_offers(1), 2)
        self.assertEqual(3, len(service.claims.live_owners()))
        self.assertEqual(["job:0", "job:1", "job:2"], fixture.started)

        for delivery in deliveries:
            delivery.answer("accepted")

        service.stopping.set()
        await asyncio.wait_for(task, 3)
        self.assertEqual((), service.claims.live_owners())
        self.assertTrue(all(session.events == ["prepare", "execute", "retire"] for session in sessions))

    async def test_each_capacity_limit_keeps_page_tail_until_owner_releases(self):
        for limits in (
            LiveAttemptLimits(1, 393216, 393216, 30),
            LiveAttemptLimits(3, 131072, 393216, 30),
            LiveAttemptLimits(3, 393216, 131072, 30),
            LiveAttemptLimits(3, 393216, 393216, 10),
        ):
            with self.subTest(limits=limits):
                (service, fixture, sessions, deliveries, entered, gates) = self.service(limits=limits, preparations=1)
                task = asyncio.create_task(service.serve())

                try:
                    for index in range(3):
                        self.assertEqual(("prepare", index), await asyncio.wait_for(entered.get(), 2))
                        self.assertEqual(index + 1, len(sessions))
                        self.assertEqual(1, len(service.claims.live_owners()))
                        self.assertEqual(1, fixture.feed_requests.qsize())
                        gates[index][0].set()
                        self.assertEqual(("execute", index), await asyncio.wait_for(entered.get(), 2))
                        gates[index][1].set()
                        await asyncio.wait_for(deliveries[index].wait_for_offers(1), 2)
                        deliveries[index].answer("accepted")

                    service.stopping.set()
                    await asyncio.wait_for(task, 3)
                    self.assertEqual(["job:0", "job:1", "job:2"], fixture.started)
                finally:
                    await self.cleanup(service, task, deliveries, gates)
