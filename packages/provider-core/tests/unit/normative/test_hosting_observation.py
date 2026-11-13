"""Execution observation conveniences preserve the original owners and facts."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from provider_core.hosting import (
    AcceptanceState,
    CallRef,
    CleanupObservation,
    DispatchState,
    Failure,
    Observation,
    OperationRef,
    Outcome,
    ResourceState,
    ResultRef,
    ResultState,
    observe_cleanup,
    observe_completion,
)


def pending_operation():
    return Observation(
        operation=OperationRef(host="host", analysis="analysis", operation_id="calculation"),
        target=CallRef(binding_id="binding", binding_revision="sha256:" + "a" * 64, name="run"),
        revision=4,
        acceptance=AcceptanceState.OWNED,
        dispatch=DispatchState.STARTED,
        finished=False,
        outcome=None,
        result_state=ResultState.NONE,
        result=None,
        states=(),
        progress=(),
        progress_dropped=0,
        failures=(),
        stop_requested=False,
        allowance_expired=False,
        resources=ResourceState.OCCUPIED,
    )


class HeldObserver:
    """Expose one owned observation that can advance while a waiter is absent."""

    def __init__(self, observation):
        self.observation = observation
        self.requests = []
        self.waiting = asyncio.Event()
        self.changed = asyncio.Event()

    async def observe(self, *, after, wait_seconds):
        self.requests.append((after, wait_seconds))
        if after == self.observation.revision and wait_seconds > 0:
            self.changed.clear()
            self.waiting.set()
            await self.changed.wait()
        return self.observation

    def publish(self, observation):
        self.observation = observation
        self.changed.set()


class HostingObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_preserves_scientific_success_and_encoding_failure(self):
        cause = ValueError("The completed scientific result cannot be encoded")
        completed = replace(
            pending_operation(),
            revision=8,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.ENCODING_FAILED,
            failures=(Failure("encoding", cause),),
            resources=ResourceState.OCCUPIED,
        )
        operation = SimpleNamespace(observe=AsyncMock(return_value=completed))

        observed = await observe_completion(operation)

        self.assertIs(completed, observed)
        self.assertIs(cause, observed.failures[0].cause)
        operation.observe.assert_awaited_once_with(after=None, wait_seconds=0)

    async def test_completion_returns_a_failed_outcome_without_raising_its_cause(self):
        cause = ArithmeticError("The numerical calculation failed")
        completed = replace(
            pending_operation(),
            finished=True,
            outcome=Outcome.FAILED,
            failures=(Failure("science", cause),),
            resources=ResourceState.UNKNOWN,
        )
        operation = SimpleNamespace(observe=AsyncMock(return_value=completed))

        self.assertIs(completed, await observe_completion(operation))
        operation.observe.assert_awaited_once_with(after=None, wait_seconds=0)

    async def test_completion_long_polls_with_the_latest_revision(self):
        pending = pending_operation()
        newer = replace(pending, revision=7, allowance_expired=True)
        result = ResultRef(
            operation=OperationRef(host="host", analysis="analysis", operation_id="calculation"),
            result_id="result",
        )
        completed = replace(
            newer,
            revision=10,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.AVAILABLE,
            result=result,
        )
        operation = SimpleNamespace(
            observe=AsyncMock(side_effect=[pending, newer, completed]),
        )

        self.assertIs(completed, await observe_completion(operation))
        self.assertEqual(
            [
                {"after": None, "wait_seconds": 0},
                {"after": 4, "wait_seconds": 1},
                {"after": 7, "wait_seconds": 1},
            ],
            [call.kwargs for call in operation.observe.await_args_list],
        )

    async def test_cancelling_completion_wait_leaves_the_original_observer_usable(self):
        pending = pending_operation()
        operation = HeldObserver(pending)
        waiter = asyncio.create_task(observe_completion(operation))
        try:
            await asyncio.wait_for(operation.waiting.wait(), 1)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIs(pending, operation.observation)

            completed = replace(pending, revision=9, finished=True, outcome=Outcome.SUCCEEDED)
            operation.publish(completed)
            self.assertIs(completed, await observe_completion(operation))
            self.assertEqual([(None, 0), (4, 1), (None, 0)], operation.requests)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_already_settled_cleanup_returns_its_observation_without_waiting(self):
        settled = CleanupObservation(
            revision=5, settled=True, allowance_expired=False, failures=(),
        )
        cleanup = SimpleNamespace(observe=AsyncMock(return_value=settled))

        self.assertIs(settled, await observe_cleanup(cleanup))
        cleanup.observe.assert_awaited_once_with(after=None, wait_seconds=0)

    async def test_cleanup_continues_observing_after_its_allowance_expires(self):
        pending = CleanupObservation(
            revision=2, settled=False, allowance_expired=False, failures=(),
        )
        expired = replace(pending, revision=6, allowance_expired=True)
        settled = replace(expired, revision=9, settled=True)
        cleanup = SimpleNamespace(observe=AsyncMock(side_effect=[pending, expired, settled]))

        self.assertIs(settled, await observe_cleanup(cleanup))
        self.assertEqual(
            [
                {"after": None, "wait_seconds": 0},
                {"after": 2, "wait_seconds": 1},
                {"after": 6, "wait_seconds": 1},
            ],
            [call.kwargs for call in cleanup.observe.await_args_list],
        )

    async def test_cancelling_cleanup_wait_leaves_the_original_control_usable(self):
        pending = CleanupObservation(
            revision=3, settled=False, allowance_expired=False, failures=(),
        )
        cleanup = HeldObserver(pending)
        waiter = asyncio.create_task(observe_cleanup(cleanup))
        try:
            await asyncio.wait_for(cleanup.waiting.wait(), 1)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIs(pending, cleanup.observation)

            settled = replace(pending, revision=8, settled=True)
            cleanup.publish(settled)
            self.assertIs(settled, await observe_cleanup(cleanup))
            self.assertEqual([(None, 0), (3, 1), (None, 0)], cleanup.requests)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_cleanup_propagates_one_original_failure_before_settlement(self):
        cause = OSError("The original cleanup could not release its resource")
        failed = CleanupObservation(
            revision=3,
            settled=False,
            allowance_expired=False,
            failures=(Failure("cleanup", cause),),
        )
        cleanup = SimpleNamespace(observe=AsyncMock(return_value=failed))

        with self.assertRaises(OSError) as raised:
            await observe_cleanup(cleanup)

        self.assertIs(cause, raised.exception)
        cleanup.observe.assert_awaited_once_with(after=None, wait_seconds=0)

    async def test_cleanup_groups_independent_original_base_exception_causes(self):
        first = OSError("The first resource failed to close")
        second = asyncio.CancelledError("The second cleanup reported cancellation")
        failed = CleanupObservation(
            revision=3,
            settled=False,
            allowance_expired=False,
            failures=(Failure("first", first), Failure("second", second)),
        )
        cleanup = SimpleNamespace(observe=AsyncMock(return_value=failed))

        with self.assertRaises(BaseExceptionGroup) as raised:
            await observe_cleanup(cleanup)

        self.assertEqual(2, len(raised.exception.exceptions))
        self.assertIs(first, raised.exception.exceptions[0])
        self.assertIs(second, raised.exception.exceptions[1])
        cleanup.observe.assert_awaited_once_with(after=None, wait_seconds=0)

    async def test_observation_errors_propagate_without_retrying(self):
        for helper in (observe_completion, observe_cleanup):
            with self.subTest(helper=helper.__name__):
                cause = OSError("The original observation channel failed")
                owner = SimpleNamespace(observe=AsyncMock(side_effect=cause))
                with self.assertRaises(OSError) as raised:
                    await helper(owner)
                self.assertIs(cause, raised.exception)
                owner.observe.assert_awaited_once_with(after=None, wait_seconds=0)


if __name__ == "__main__":
    unittest.main()
