"""A server-directed retry floor cannot be bypassed by condition recovery."""

import asyncio
import itertools
import unittest
from unittest.mock import Mock

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalOperation
from provider_core.execution import ConditionRecovery, ConditionRecoveryExpired, ExecutionFailed, ExecutionStopped
from tests.fakes.api import api, reply, response_headers
from tests.fakes.attempts import observation
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.condition_recovery import ConditionOutageTransport
from tests.fakes.conditions import ReportingSession
from tests.fakes.execution import live_attempt, live_executor


def unavailable_with_retry_after():
    return reply(b"unavailable", status=503, headers=(
        *response_headers(status=503, request_id=b"request:test"), (b"retry-after", b"1"),
    ))


def make_executor(store, claim, session, transport, *, window, on_retry=None):
    return live_executor(
        api(transport, store.scope.api_origin), store, claim, session,
        delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
        delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01,
        task_settle_seconds=0.1, condition_recovery=ConditionRecovery(window, 0.04, on_retry or Mock(), Mock()),
    )


class ConditionRetryAfterTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_floor_beyond_window_expires_without_a_second_put(self):
        store, acknowledged, claim = await live_attempt()
        transport = ConditionOutageTransport(first_failed_put=2)
        transport.faults = itertools.repeat(unavailable_with_retry_after())
        session = ReportingSession(store)
        on_retry = Mock()
        executor = make_executor(store, claim, session, transport, window=0.1, on_retry=on_retry)
        with self.assertRaises(ExecutionFailed) as caught:
            await asyncio.wait_for(executor.run(acknowledged), 2)
        self.assertIs(type(caught.exception.cause), ConditionRecoveryExpired)
        original = caught.exception.cause.cause
        self.assertIs(type(original), ApiFailure)
        self.assertEqual(original.status, 503)
        self.assertEqual(original.retry_after_seconds, 1)
        self.assertEqual(len(transport.condition_requests), 1,
                         "The same condition must not be sent before the server's retry floor")
        on_retry.assert_not_called()
        self.assertIs(caught.exception.condition_failure, original)
        self.assertEqual([event[0] for event in session.events], ["retire"])
        claim.release(owner=claim.lifecycle_owner)

    async def test_retry_floor_within_window_precedes_the_replayed_put(self):
        class TimedTransport(ConditionOutageTransport):
            def __init__(self):
                super().__init__(first_failed_put=2)
                self.put_times = []
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if request.method == "PUT":
                    self.put_times.append(asyncio.get_running_loop().time())
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        store, acknowledged, claim = await live_attempt()
        transport = TimedTransport()
        transport.faults = iter((unavailable_with_retry_after(),))
        session = ReportingSession(store)
        executor = make_executor(store, claim, session, transport, window=2)
        terminal = await asyncio.wait_for(executor.run(acknowledged), 4)
        self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
        self.assertEqual(len(transport.condition_requests), 2)
        self.assertGreaterEqual(transport.put_times[2] - transport.put_times[1], 0.99)
        self.assertEqual(transport.condition_requests[0].content, transport.condition_requests[1].content)
        claim.release(owner=claim.lifecycle_owner)

    async def test_server_stop_interrupts_the_advised_wait(self):
        store, acknowledged, claim = await live_attempt()
        transport = ConditionOutageTransport(first_failed_put=2)
        transport.faults = itertools.repeat(unavailable_with_retry_after())
        transport.followup_observation = reply(observation(job_state="cancelled"))
        session = ReportingSession(store)
        executor = make_executor(store, claim, session, transport, window=2)
        with self.assertRaises(ExecutionFailed) as caught:
            await asyncio.wait_for(executor.run(acknowledged), 0.5)
        self.assertIs(type(caught.exception.cause), ExecutionStopped)
        self.assertEqual(len(transport.condition_requests), 1)
        self.assertEqual([event[0] for event in session.events], ["retire"])
        claim.release(owner=claim.lifecycle_owner)
