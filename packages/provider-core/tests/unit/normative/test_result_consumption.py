"""Keep original acceptance and release owners through interrupted observers."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from provider_core.hosting import (
    CleanupObservation, Failure, OperationRef, ResultConsumption, ResultRef,
)


class ResultConsumptionTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, receive, *, release=None):
        reference = ResultRef(
            operation=OperationRef(host="host", analysis="analysis", operation_id="calculation"),
            result_id="result",
        )
        if release is None:
            release = SimpleNamespace(observe=AsyncMock(return_value=CleanupObservation(
                revision=1, settled=True, allowance_expired=False, failures=(),
            )))
        operation = SimpleNamespace(
            read_result=AsyncMock(return_value=b"original result"),
            release_result=Mock(return_value=release),
        )
        return ResultConsumption(operation=operation, result=reference, receive=receive)

    async def test_cancelled_waiter_joins_original_acceptance_after_receiver_effect(self):
        effect = asyncio.Event()
        acknowledge = asyncio.Event()
        effects = []

        async def receive(payload):
            effects.append(payload)
            effect.set()
            await acknowledge.wait()

        owner = self.owner(receive)
        first = asyncio.create_task(owner.consume())
        second = None
        try:
            async with asyncio.timeout(5):
                await effect.wait()
            acceptance = owner.acceptance
            with self.assertRaises(AttributeError):
                owner.result = replace(owner.result, result_id="other-result")
            with self.assertRaises(AttributeError):
                owner.operation = SimpleNamespace()
            with self.assertRaises(AttributeError):
                owner.receive = AsyncMock()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(acceptance.cancelled())
            self.assertIs(acceptance, owner.start())
            owner.operation.release_result.assert_not_called()
            second = asyncio.create_task(owner.consume())
            acknowledge.set()
            async with asyncio.timeout(5):
                await second
            self.assertEqual([b"original result"], effects)
            owner.operation.read_result.assert_awaited_once_with(owner.result)
            owner.operation.release_result.assert_called_once_with(owner.result)
        finally:
            acknowledge.set()
            await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
            await owner.wait_for_acceptance()

    async def test_receiver_failure_after_effect_is_retained_without_resend_or_release(self):
        effects = []
        cause = OSError("Receiver acknowledgement was lost after its external effect")

        async def receive(payload):
            effects.append(payload)
            raise cause

        owner = self.owner(receive)
        for _ in range(2):
            with self.assertRaises(OSError) as raised:
                await owner.consume()
            self.assertIs(cause, raised.exception)
        self.assertEqual([b"original result"], effects)
        owner.operation.read_result.assert_awaited_once_with(owner.result)
        owner.operation.release_result.assert_not_called()

    async def test_known_acceptance_survives_synchronous_release_request_failure(self):
        receive = AsyncMock()
        owner = self.owner(receive)
        original_release = owner.operation.release_result.return_value
        cause = OSError("Exact release request was interrupted")
        owner.operation.release_result.side_effect = [cause, original_release]
        with self.assertRaises(OSError) as raised:
            await owner.consume()
        self.assertIs(cause, raised.exception)
        self.assertIsNone(owner.release)
        await owner.consume()
        receive.assert_awaited_once_with(b"original result")
        self.assertIs(original_release, owner.release)
        self.assertEqual([((owner.result,), {}), ((owner.result,), {})], owner.operation.release_result.call_args_list)

    async def test_cancelled_release_observer_keeps_the_original_control(self):
        waiting = asyncio.Event()
        settled = asyncio.Event()

        async def observe(*, after, wait_seconds):
            waiting.set()
            await settled.wait()
            return CleanupObservation(revision=1, settled=True, allowance_expired=False, failures=())

        release = SimpleNamespace(observe=AsyncMock(side_effect=observe))
        receive = AsyncMock()
        owner = self.owner(receive, release=release)
        waiter = asyncio.create_task(owner.consume())
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIs(release, owner.release)
            settled.set()
            await owner.consume()
            receive.assert_awaited_once_with(b"original result")
            owner.operation.release_result.assert_called_once_with(owner.result)
        finally:
            settled.set()
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_failed_release_reobserves_original_failure_without_reaccepting(self):
        cause = OSError("Result disposal failed")
        release = SimpleNamespace(observe=AsyncMock(return_value=CleanupObservation(
            revision=1, settled=False, allowance_expired=False,
            failures=(Failure("cleanup", cause),),
        )))
        receive = AsyncMock()
        owner = self.owner(receive, release=release)
        for _ in range(2):
            with self.assertRaises(OSError) as raised:
                await owner.consume()
            self.assertIs(cause, raised.exception)
        receive.assert_awaited_once_with(b"original result")
        owner.operation.release_result.assert_called_once_with(owner.result)
        self.assertIs(release, owner.release)

    async def test_concurrent_observers_share_acceptance_and_release(self):
        receive = AsyncMock()
        owner = self.owner(receive)
        await asyncio.gather(owner.consume(), owner.consume())
        receive.assert_awaited_once_with(b"original result")
        owner.operation.read_result.assert_awaited_once_with(owner.result)
        owner.operation.release_result.assert_called_once_with(owner.result)

    async def test_receiver_cancellation_never_releases_or_restarts(self):
        cause = asyncio.CancelledError("Receiver ended its own original attempt")
        receive = AsyncMock(side_effect=cause)
        owner = self.owner(receive)
        for _ in range(2):
            with self.assertRaises(asyncio.CancelledError):
                await owner.consume()
        receive.assert_awaited_once_with(b"original result")
        owner.operation.release_result.assert_not_called()

    async def test_receiver_process_exit_stays_with_its_original_owned_task(self):
        cause = SystemExit("Receiver requested process exit")
        receive = AsyncMock(side_effect=cause)
        owner = self.owner(receive)
        for _ in range(2):
            with self.assertRaises(BaseExceptionGroup) as raised:
                await owner.consume()
            self.assertIs(cause, raised.exception.exceptions[0])
        receive.assert_awaited_once_with(b"original result")
        owner.operation.release_result.assert_not_called()

    async def test_result_read_failure_never_invokes_receiver_or_retries_read(self):
        cause = OSError("Original retained result could not be read")
        receive = AsyncMock()
        owner = self.owner(receive)
        owner.operation.read_result.side_effect = cause
        for _ in range(2):
            with self.assertRaises(OSError) as raised:
                await owner.consume()
            self.assertIs(cause, raised.exception)
        owner.operation.read_result.assert_awaited_once_with(owner.result)
        receive.assert_not_awaited()
        owner.operation.release_result.assert_not_called()

    async def test_acceptance_can_be_joined_without_authorizing_release(self):
        receive = AsyncMock()
        owner = self.owner(receive)
        self.assertIs(owner.start(), owner.start())
        await owner.wait_for_acceptance()
        receive.assert_awaited_once_with(b"original result")
        owner.operation.release_result.assert_not_called()

    async def test_shutdown_of_an_unstarted_owner_has_no_receiver_effect(self):
        receive = AsyncMock()
        owner = self.owner(receive)
        self.assertIsNone(await owner.settle())
        self.assertIsNone(owner.acceptance)
        receive.assert_not_awaited()
        owner.operation.read_result.assert_not_awaited()
        owner.operation.release_result.assert_not_called()

    async def test_shutdown_defers_repeated_cancellation_until_acceptance_and_release(self):
        entered = asyncio.Event()
        acknowledge = asyncio.Event()
        shutdown_waiting = asyncio.Event()
        calls = []

        async def receive(payload):
            calls.append(payload)
            entered.set()
            await acknowledge.wait()

        owner = self.owner(receive)
        owner.start()
        async with asyncio.timeout(5):
            await entered.wait()
        original_join = owner.wait_for_acceptance

        async def observed_join():
            shutdown_waiting.set()
            await original_join()

        with unittest.mock.patch.object(ResultConsumption, "wait_for_acceptance", side_effect=observed_join):
            shutdown = asyncio.create_task(owner.settle())
            try:
                async with asyncio.timeout(5):
                    await shutdown_waiting.wait()
                shutdown.cancel("first shutdown interruption")
                # call_soon executes after the first cancellation is delivered;
                # no wall-clock sleep guesses whether the receiver has returned.
                asyncio.get_running_loop().call_soon(shutdown.cancel, "second shutdown interruption")
                asyncio.get_running_loop().call_soon(acknowledge.set)
                with self.assertRaises(BaseExceptionGroup) as raised:
                    await shutdown
                self.assertEqual(2, len(raised.exception.exceptions))
                self.assertTrue(all(isinstance(cause, asyncio.CancelledError) for cause in raised.exception.exceptions))
                self.assertTrue(owner.acceptance.done())
                owner.operation.release_result.assert_called_once_with(owner.result)
                self.assertEqual([b"original result"], calls)
            finally:
                acknowledge.set()
                await asyncio.gather(shutdown, return_exceptions=True)
                await owner.wait_for_acceptance()

    async def test_shutdown_observes_pending_release_and_preserves_failure_with_interruption(self):
        waiting = asyncio.Event()
        finish = asyncio.Event()
        cause = OSError("Original release failed during shutdown")

        async def observe(*, after, wait_seconds):
            waiting.set()
            await finish.wait()
            return CleanupObservation(
                revision=1, settled=False, allowance_expired=False,
                failures=(Failure("cleanup", cause),),
            )

        receive = AsyncMock()
        release = SimpleNamespace(observe=AsyncMock(side_effect=observe))
        owner = self.owner(receive, release=release)
        owner.start()
        shutdown = asyncio.create_task(owner.settle())
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
            shutdown.cancel("shutdown observer cancelled")
            finish.set()
            with self.assertRaises(BaseExceptionGroup) as raised:
                await shutdown
            self.assertIsInstance(raised.exception.exceptions[0], asyncio.CancelledError)
            self.assertIs(cause, raised.exception.exceptions[1])
            owner.operation.release_result.assert_called_once_with(owner.result)
            receive.assert_awaited_once_with(b"original result")
            self.assertIs(release, owner.release)
        finally:
            finish.set()
            await asyncio.gather(shutdown, return_exceptions=True)

    async def test_receiver_cancellation_is_terminal_during_shutdown(self):
        receive = AsyncMock(side_effect=asyncio.CancelledError("receiver cancelled itself"))
        owner = self.owner(receive)
        owner.start()
        async with asyncio.timeout(5):
            with self.assertRaises(asyncio.CancelledError):
                await owner.settle()
        receive.assert_awaited_once_with(b"original result")
        owner.operation.release_result.assert_not_called()
