"""Per-job ownership survives shared calls, malformed replies and cancellation."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from provider_core.hosting import CleanupObservation
from provider_core.batch_call import BatchCall, BatchPolicy
from provider_core.payload import JsonDataclassCodec
from provider_core.scientific_call import ErrorPolicy
from provider_core.process_call import ProcessCall


@dataclass
class Input:
    value: int


class Call(ProcessCall):
    """A gated call port with the same pre-effect permission and cleanup contract."""

    def __init__(self):
        self.settings = SimpleNamespace(error_policy=ErrorPolicy())
        self.closed = False
        self._invocation = self
        self._batch_owner = None
        self._active_calls = 0
        self.host = self
        self.retirements = 0
        self.retirement_observation = CleanupObservation(0, True, False, ())
        self.started = asyncio.Queue()
        self.admit = asyncio.Event()
        self.release = asyncio.Event()
        self.clean = asyncio.Event()

        self.clean.set()
        self.admit.set()
        self.cancelled = asyncio.Event()
        self.calls = []
        self.failure = None

    def retire(self):
        self.retirements += 1

        return self

    async def observe(self, *, after, wait_seconds):
        return self.retirement_observation

    async def __call__(self, *, scientific_input, require_execution, _batch_owner=None):
        await self.admit.wait()
        require_execution()
        self.calls.append(scientific_input)
        self.started.put_nowait(scientific_input)

        try:
            await self.release.wait()

            if self.failure is not None:
                raise self.failure

            # repo-check: allow-tuple-generator -- The fixture returns one immutable ordered batch reply.
            return tuple((sample.value * 10 + index) for (index, sample) in enumerate(scientific_input))
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            await self.clean.wait()


class Permission:
    def __init__(self):
        self.revoked = False

    def __call__(self):
        if self.revoked:
            raise PermissionError("The fixture Attempt was revoked")


class BatchCallTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, *, entries=2, seconds=0.02, budget=1024, unpack=tuple):
        call = Call()

        owner = BatchCall(
            call=call,
            policy=BatchPolicy(
                maximum_entries=entries,
                collection_seconds=seconds,
                maximum_queued_input_bytes=budget,
                maximum_entry_input_bytes=budget,
            ),
            entry_codec=JsonDataclassCodec(format="test.input.v1", value_type=Input),
            pack=tuple,
            unpack=unpack,
        )

        return (owner, call)

    async def activate(self, owner, value, permission=None):
        entry = await owner.prepare(Input(value))
        task = asyncio.create_task(entry.execute(require_execution=permission or Permission()))

        return (entry, task)

    async def test_size_flush_routes_equal_values_by_participation_and_freezes_input(self):
        (owner, call) = self.owner(seconds=10)

        async with owner:
            value = Input(2)
            first = await owner.prepare(value)
            value.value = 99
            (second, second_task) = await self.activate(owner, 2)
            first_task = asyncio.create_task(first.execute(require_execution=Permission()))
            values = await asyncio.wait_for(call.started.get(), 1)

            self.assertEqual([2, 2], [item.value for item in values])
            call.release.set()
            self.assertEqual([20, 21], await asyncio.gather(second_task, first_task))
            await first.retire()
            await second.retire()
            self.assertEqual(0, owner.input_bytes)
            self.assertEqual(1, len(call.calls))

    async def test_deadline_flush_excludes_prepared_but_inactive_entries(self):
        (owner, call) = self.owner(entries=3)

        async with owner:
            inactive = await owner.prepare(Input(8))
            (active, task) = await self.activate(owner, 1)
            values = await asyncio.wait_for(call.started.get(), 1)

            self.assertEqual([1], [item.value for item in values])
            call.release.set()
            self.assertEqual(10, await task)
            await active.retire()
            await inactive.retire()

            with self.assertRaises(RuntimeError):
                await inactive.execute(require_execution=Permission())

    async def test_queue_byte_budget_includes_running_input(self):
        (owner, call) = self.owner(entries=1, budget=len(b'{"value":1}'))

        async with owner:
            (first, task) = await self.activate(owner, 1)

            await asyncio.wait_for(call.started.get(), 1)
            pending = asyncio.create_task(owner.prepare(Input(2)))

            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.assertEqual(owner.policy.maximum_queued_input_bytes, owner.input_bytes)
            call.release.set()
            await task
            second = await asyncio.wait_for(pending, 1)

            await first.retire()
            await second.retire()
            self.assertEqual(0, owner.input_bytes)

    async def test_revocation_at_call_boundary_rebuilds_only_undispatched_members(self):
        (owner, call) = self.owner(seconds=10)

        call.admit.clear()

        async with owner:
            permission = Permission()
            (first, first_task) = await self.activate(owner, 1, permission)
            (second, second_task) = await self.activate(owner, 2)

            # Observe collection via the owned invocation rather than waiting a timer.
            while owner.invocation is None:
                await asyncio.sleep(0)

            permission.revoked = True

            call.admit.set()
            values = await asyncio.wait_for(call.started.get(), 1)

            self.assertEqual([2], [item.value for item in values])

            with self.assertRaises(PermissionError):
                await first_task

            call.release.set()
            self.assertEqual(20, await second_task)
            await first.retire()
            await second.retire()
            self.assertEqual(1, len(call.calls))

    async def test_cancelled_waiter_and_participant_do_not_stop_a_sibling(self):
        (owner, call) = self.owner(seconds=10)

        async with owner:
            (first, first_task) = await self.activate(owner, 1)
            (second, second_task) = await self.activate(owner, 2)

            await asyncio.wait_for(call.started.get(), 1)
            first_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await first_task

            retiring = asyncio.create_task(first.retire())

            await asyncio.sleep(0)
            self.assertFalse(retiring.done())
            self.assertFalse(call.cancelled.is_set())
            self.assertGreater(owner.input_bytes, 0)
            call.release.set()
            self.assertEqual(21, await second_task)
            await retiring
            await second.retire()

    async def test_all_retired_entries_stop_once_and_join_shared_cleanup(self):
        (owner, call) = self.owner(seconds=10)

        async with owner:
            (first, first_task) = await self.activate(owner, 1)
            (second, second_task) = await self.activate(owner, 2)

            await asyncio.wait_for(call.started.get(), 1)
            call.clean.clear()
            retiring = asyncio.gather(first.retire(), second.retire())

            await asyncio.wait_for(call.cancelled.wait(), 1)
            self.assertFalse(retiring.done())
            self.assertGreater(owner.input_bytes, 0)
            call.clean.set()
            await asyncio.wait_for(retiring, 1)
            outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)

            self.assertTrue(all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes))
            self.assertEqual(0, owner.input_bytes)

    async def test_global_close_joins_cleanup_and_wakes_prepared_handles(self):
        (owner, call) = self.owner(entries=1)

        await owner.__aenter__()
        (first, task) = await self.activate(owner, 1)

        await asyncio.wait_for(call.started.get(), 1)
        inactive = await owner.prepare(Input(2))

        call.clean.clear()
        closing = asyncio.create_task(owner.close())

        await asyncio.wait_for(call.cancelled.wait(), 1)
        self.assertFalse(closing.done())
        closing.cancel()
        call.clean.set()

        with self.assertRaises(asyncio.CancelledError):
            await closing

        await owner.close()
        await first.retire()
        await inactive.retire()

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_entire_reply_must_validate_before_any_result_is_routed(self):
        def invalid_later(reply):
            yield reply[0]
            raise ValueError("The later result is invalid")

        for unpack in (lambda reply: reply[:1], invalid_later):
            with self.subTest(unpack=unpack):
                (owner, call) = self.owner(unpack=unpack)

                await owner.__aenter__()
                (first, first_task) = await self.activate(owner, 1)
                (second, second_task) = await self.activate(owner, 2)

                await asyncio.wait_for(call.started.get(), 1)
                call.release.set()
                results = await asyncio.gather(first_task, second_task, return_exceptions=True)

                self.assertTrue(all(isinstance(result, ValueError) for result in results))
                self.assertIs(results[0], results[1])

                for entry in (first, second):
                    await entry.retire()

                with self.assertRaises(ValueError):
                    await owner.close()

                self.assertEqual(1, len(call.calls))

    async def test_lost_reply_never_replays_or_guesses_an_entry_result(self):
        (owner, call) = self.owner()
        failure = ConnectionError("Whole reply lost after the first entry ran")
        call.failure = failure

        await owner.__aenter__()
        (first, first_task) = await self.activate(owner, 1)
        (second, second_task) = await self.activate(owner, 2)

        await asyncio.wait_for(call.started.get(), 1)
        call.release.set()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

        self.assertTrue(all((result is failure) for result in results))

        for entry in (first, second):
            await entry.retire()

        with self.assertRaises(ConnectionError):
            await owner.close()

        self.assertEqual(1, len(call.calls))

    async def test_close_before_collector_starts_retires_prepared_entries(self):
        (owner, call) = self.owner()

        await owner.__aenter__()
        entry = await owner.prepare(Input(1))

        await owner.close()
        await asyncio.wait_for(entry.retire(), 1)
        self.assertEqual([], call.calls)
        self.assertEqual(0, owner.input_bytes)

    async def test_cleanup_failure_retains_only_unsettled_participations(self):
        for settled in (False, True):
            with self.subTest(settled=settled):
                (owner, call) = self.owner(entries=1)
                cleanup = RuntimeError("Fixture retirement failed")
                failure = ValueError("Fixture whole reply was invalid")
                call.failure = failure
                call.retirement_observation = CleanupObservation(1, settled, False, (SimpleNamespace(cause=cleanup),))

                await owner.__aenter__()
                (entry, task) = await self.activate(owner, 1)

                await asyncio.wait_for(call.started.get(), 1)
                call.release.set()

                with self.assertRaises(ValueError):
                    await task

                if settled:
                    await entry.retire()
                    self.assertEqual(0, owner.input_bytes)
                else:
                    with self.assertRaises(RuntimeError):
                        await entry.retire()

                    self.assertGreater(owner.input_bytes, 0)

                with self.assertRaises(ExceptionGroup) as caught:
                    await owner.close()

                self.assertEqual([failure, cleanup], list(caught.exception.exceptions))
                self.assertEqual(1, call.retirements)

    async def test_interrupted_fatal_retirement_preserves_original_failure_and_receipt(self):
        (owner, call) = self.owner(entries=1)
        failure = ValueError("Whole reply failed")
        call.failure = failure
        retiring = asyncio.Event()
        retired = asyncio.Event()

        async def observe(*, after, wait_seconds):
            retiring.set()
            await retired.wait()

            return call.retirement_observation

        call.observe = observe

        await owner.__aenter__()
        (entry, task) = await self.activate(owner, 1)

        await asyncio.wait_for(call.started.get(), 1)
        call.release.set()
        await asyncio.wait_for(retiring.wait(), 1)
        closing = asyncio.create_task(owner.close())

        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.assertFalse(entry.settled.done())
        retired.set()

        with self.assertRaises(ValueError) as caught:
            await task

        self.assertIs(failure, caught.exception)
        await entry.retire()

        with self.assertRaises(BaseExceptionGroup) as caught:
            await closing

        self.assertIn(failure, caught.exception.exceptions)
        self.assertTrue(owner.retirement_settled)
        self.assertEqual(0, owner.input_bytes)
