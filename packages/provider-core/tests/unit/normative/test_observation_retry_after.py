"""Retry-After pacing remains inside the original active-observation gap."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from provider_core.execution import ObservationGapExpired, ObservationRecovery
from tests.fakes.api import Transport, api, reply, response_headers
from tests.fakes.attempts import observation
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.execution import Session, live_attempt, live_executor


class ObservationRetryAfterTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, gap, observer=None):
        probe, acknowledged, claim = await live_attempt()
        clock = SimpleNamespace(now=100.0)
        reads, waits, notices = [], [], []
        headers = (*response_headers(status=429, request_id=b"request:rate"), (b"retry-after", b"1"))

        class TimedTransport(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                reads.append(clock.now)
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = TimedTransport(reply(b"PRIVATE server body", status=429, headers=headers), reply(observation()))

        def retry(_attempt, failure, seconds):
            notices.append((failure, seconds))
            if observer is not None:
                observer(clock)

        executor = live_executor(api(transport, probe.scope.api_origin), probe, claim, Session(probe),
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=.01,
            delivery_retry_max_seconds=.04, allowed_job_states=frozenset({"open"}), poll_seconds=.1,
            task_settle_seconds=.1, observation_recovery=ObservationRecovery(gap, .2, retry, Mock()))
        executor._record = executor._acknowledged = acknowledged
        return executor, clock, reads, waits, notices

    def clock(self, clock, waits):
        namespace = SimpleNamespace(**vars(asyncio))
        namespace.get_running_loop = lambda: SimpleNamespace(time=lambda: clock.now)

        async def wait(awaitable, *, timeout):
            waits.append(timeout)
            awaitable.close()
            clock.now += timeout
            raise TimeoutError

        namespace.wait_for = wait
        return patch("provider_core.execution.asyncio", namespace)

    async def test_retry_after_floor_precedes_the_next_actual_read(self):
        executor, clock, reads, waits, notices = await self.fixture(gap=3)
        with self.clock(clock, waits), patch("provider_core.execution.random.uniform", return_value=.1):
            result = await executor._observe_until_available(clock.now + 3)
        self.assertEqual(result.state, "in_progress")
        self.assertEqual(reads, [100.0, 101.0])
        self.assertEqual(waits, [1.0])
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][1], 1.0)

    async def test_unfit_floor_waits_for_original_gap_without_retry_or_notice(self):
        executor, clock, reads, waits, notices = await self.fixture(gap=.5)
        with self.clock(clock, waits), patch("provider_core.execution.random.uniform", return_value=.1):
            with self.assertRaises(ObservationGapExpired) as caught:
                await executor._observe_until_available(clock.now + .5)
        self.assertEqual(clock.now, 100.5)
        self.assertEqual(reads, [100.0])
        self.assertEqual(waits, [.5])
        self.assertEqual(notices, [])
        self.assertIs(caught.exception.cause, executor.api.last_failure)
        self.assertEqual(caught.exception.cause.retry_after_seconds, 1.0)

    async def test_protected_observer_cannot_renew_gap_or_retry_after_its_expiry(self):
        def consume_gap(clock):
            clock.now += 3

        executor, clock, reads, waits, notices = await self.fixture(gap=2, observer=consume_gap)
        with self.clock(clock, waits), patch("provider_core.execution.random.uniform", return_value=.1):
            with self.assertRaises(ObservationGapExpired) as caught:
                await executor._observe_until_available(102.0)
        self.assertEqual(reads, [100.0])
        self.assertEqual(waits, [])
        self.assertEqual(len(notices), 1)
        self.assertIs(caught.exception.cause, notices[0][0])
