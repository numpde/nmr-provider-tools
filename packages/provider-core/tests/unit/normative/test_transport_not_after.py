"""An inherited absolute cap never creates another cleanup wait or task."""

import asyncio
import ssl
import tempfile
import time
import unittest
from unittest.mock import patch

import httpcore
import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.transport import Client, Stream


class TransportNotAfterTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, transport, mode, descriptor, **kwargs):
        request = httpx.Request("GET", "https://model.example/turn")
        if mode == "file":
            return await transport.exchange_file(request, output_fd=descriptor, maximum_bytes=8, **kwargs)
        if mode == "decoded":
            return await transport.exchange_decoded(request, maximum_bytes=8, error_maximum_bytes=4, **kwargs)
        return await transport.exchange(request, maximum_bytes=8, **kwargs)

    async def test_expired_cap_cannot_dispatch_network_or_file_work(self):
        for mode in ("raw", "file", "decoded"):
            with self.subTest(mode=mode), tempfile.TemporaryFile() as output:
                output.write(b"sentinel")
                output.flush()
                transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.3)
                with patch("provider_core.transport.httpcore.AsyncConnectionPool") as pool, patch(
                    "provider_core.transport._FileBody"
                ) as file_body:
                    with self.assertRaises(ExchangeFailed) as caught:
                        await self.exchange(transport, mode, output.fileno(), not_after=asyncio.get_running_loop().time()-1)
                pool.assert_not_called()
                file_body.assert_not_called()
                self.assertIs(transport.last_failure, caught.exception)
                self.assertTrue(transport.retirement_confirmed)
                self.assertIsInstance(caught.exception.primary, TimeoutError)
                self.assertIsNone(caught.exception.network_failure)
                output.seek(0)
                self.assertEqual(output.read(), b"sentinel")

    async def test_completed_response_is_not_admitted_when_observation_resumes_after_cap(self):
        client = Client(Stream([b"complete"]))
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.3)
        loop = asyncio.get_running_loop()
        cap = loop.time() + 0.03
        original_wait = asyncio.wait
        tasks = []

        async def delayed_observer(selected, *, timeout):
            result = await original_wait(selected, timeout=timeout)
            tasks.extend(selected)
            # Delay only observation after the actual original exchange completes.
            await asyncio.sleep(max(0, cap - loop.time()) + 0.01)
            return result

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client), patch(
            "provider_core.transport.asyncio.wait", side_effect=delayed_observer,
        ):
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange(httpx.Request("GET", "https://model.example/turn"),
                                         maximum_bytes=8, not_after=cap)
        self.assertIsInstance(caught.exception.primary, TimeoutError)
        self.assertEqual(caught.exception.response_head.status, 200)
        self.assertTrue(transport.retirement_confirmed)
        self.assertTrue(tasks[0].done())
        self.assertEqual(tasks[0].result().response.body, b"complete")
        self.assertTrue(client.closed)

    async def test_original_task_queued_beyond_cap_never_enters_resource_work(self):
        for mode in ("raw", "file", "decoded"):
            with self.subTest(mode=mode), tempfile.TemporaryFile() as output:
                loop = asyncio.get_running_loop()
                transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.3)
                with patch("provider_core.transport.httpcore.AsyncConnectionPool") as pool, patch(
                    "provider_core.transport._FileBody"
                ) as file_body:
                    loop.call_soon(time.sleep, 0.03)
                    with self.assertRaises(ExchangeFailed):
                        await self.exchange(transport, mode, output.fileno(), not_after=loop.time()+0.01)
                pool.assert_not_called()
                file_body.assert_not_called()
                self.assertTrue(transport.retirement_confirmed)

    async def test_cap_returns_without_new_cleanup_budget_and_late_failure_stays_original(self):
        release, entered, heads = asyncio.Event(), asyncio.Event(), []
        original = httpcore.ReadError("protected original late failure")
        class LateRead(Stream):
            async def __aiter__(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                    raise original
                yield b"unreachable"
        client = Client(LateRead([]))
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.3)
        owner = object()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            try:
                with self.assertRaises(ExchangeFailed) as caught:
                    await transport.exchange(httpx.Request("GET", "https://model.example/turn"), maximum_bytes=8,
                                             owner=owner, before_body=heads.append, not_after=started+0.03)
                self.assertLess(loop.time()-started, 0.1)
                self.assertTrue(entered.is_set())
                self.assertFalse(transport.retirement_confirmed)
                self.assertIs(transport.pending_owner, owner)
                exchange = transport._active
                for _ in range(2):
                    with self.assertRaises(ExchangeFailed) as repeated:
                        await asyncio.wait_for(transport.retire(owner=owner), 0.05)
                    self.assertIs(repeated.exception, caught.exception)
                    self.assertIs(transport._active, exchange)
                release.set()
                await asyncio.wait_for(asyncio.gather(exchange.task), 0.2)
                await transport.retire(owner=owner)
                self.assertTrue(transport.retirement_confirmed)
                self.assertIs(transport.last_failure, caught.exception)
                self.assertIs(caught.exception.primary, original)
                self.assertIs(caught.exception.network_failure, original)
                self.assertIs(caught.exception.response_head, heads[0])
                self.assertEqual(len(heads), 1)
                self.assertTrue(client.closed)
            finally:
                release.set()
                if transport._active is not None:
                    await asyncio.gather(transport._active.task, return_exceptions=True)
                    await transport.retire(owner=owner)

    async def test_actual_cancellation_keeps_identity_and_owner_after_absolute_cap(self):
        release, entered, cancellations = asyncio.Event(), asyncio.Event(), []
        class HeldClose(Stream):
            async def aclose(self):
                entered.set()
                await release.wait()
                await super().aclose()
        client = Client(HeldClose([], stall=asyncio.Event()))
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.3)
        owner = object()
        async def call():
            try:
                await transport.exchange(httpx.Request("GET", "https://model.example/turn"), maximum_bytes=8,
                                         owner=owner, not_after=asyncio.get_running_loop().time()+0.04)
            except asyncio.CancelledError as cancellation:
                cancellations.append(cancellation)
                raise
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            task = asyncio.create_task(call())
            try:
                await asyncio.wait_for(client.sent.wait(), 0.2)
                task.cancel("original caller interruption")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task,0.1)
                self.assertIs(caught.exception,cancellations[0])
                self.assertIs(transport.pending_owner,owner)
                self.assertFalse(transport.retirement_confirmed)
                self.assertTrue(entered.is_set())
                original_task = transport._active.task
            finally:
                release.set()
                await asyncio.gather(task,return_exceptions=True)
                if transport._active is not None:
                    await asyncio.gather(transport._active.task,return_exceptions=True)
                    await transport.retire(owner=owner)
            self.assertTrue(original_task.done())
            self.assertTrue(transport.retirement_confirmed)

    async def test_existing_outer_deadline_can_bound_the_same_original_cleanup(self):
        release = asyncio.Event()
        class HeldClose(Stream):
            async def aclose(self):
                await release.wait()
                await super().aclose()
        client = Client(HeldClose([],stall=asyncio.Event()))
        transport = HttpTransport(trust=ssl.create_default_context(),exchange_seconds=1,retirement_seconds=0.3)
        loop=asyncio.get_running_loop()
        started=loop.time()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool",return_value=client):
            try:
                with self.assertRaises((TimeoutError,ExchangeFailed)):
                    async with asyncio.timeout_at(started+0.02):
                        await transport.exchange(httpx.Request("GET","https://model.example/turn"),maximum_bytes=8,
                                                 not_after=started+0.02)
                self.assertLess(loop.time()-started,0.08)
                self.assertFalse(transport.retirement_confirmed)
            finally:
                release.set()
                if transport._active is not None:
                    await asyncio.gather(transport._active.task,return_exceptions=True)
                    await transport.retire()

    async def test_first_caller_cancellation_survives_another_cancellation_during_original_close(self):
        await self.repeated_cancellations(first_during_request=True)

    async def test_first_retirement_cancellation_survives_another_cancellation_in_the_same_wait(self):
        await self.repeated_cancellations(first_during_request=False)

    async def repeated_cancellations(self, *, first_during_request):
        close_entered, release_close = asyncio.Event(), asyncio.Event()
        first_seen = asyncio.Event()
        cancellations = []
        prior_cause = LookupError("protected preexisting caller cause")
        native_failure = httpcore.ReadError("protected original network failure")
        close_failure = OSError("protected independent late close failure")

        class HeldClose(Stream):
            async def __aiter__(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise native_failure
                yield b"unreachable"

            async def aclose(self):
                close_entered.set()
                await release_close.wait()
                await super().aclose()

        client = Client(HeldClose([]), close_failure=close_failure)
        seconds = 1
        if not first_during_request:
            seconds = 0.01
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=seconds,
                                  retirement_seconds=0.3)
        loop = asyncio.get_running_loop()
        owner = object()
        cap = loop.time() + 0.08
        original_wait = asyncio.wait
        caller = None

        async def observe_cancellation(tasks, *, timeout):
            try:
                return await original_wait(tasks, timeout=timeout)
            except asyncio.CancelledError as cancellation:
                if asyncio.current_task() is caller:
                    if not cancellations:
                        cancellation.__cause__ = prior_cause
                        first_seen.set()
                    cancellations.append(cancellation)
                raise

        request = httpx.Request("GET", "https://model.example/turn")
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client), patch(
            "provider_core.transport.asyncio.wait", side_effect=observe_cancellation,
        ):
            caller = asyncio.create_task(transport.exchange(request, maximum_bytes=8, owner=owner, not_after=cap))
            try:
                await asyncio.wait_for(client.sent.wait(), 0.2)
                if not first_during_request:
                    await asyncio.wait_for(close_entered.wait(), 0.2)
                caller.cancel("first actual caller cancellation")
                await asyncio.wait_for(first_seen.wait(), 0.2)
                await asyncio.wait_for(close_entered.wait(), 0.2)
                await asyncio.sleep(0)
                original = transport._active
                caller.cancel("second actual caller cancellation")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(caller, 0.2)
                self.assertEqual(len(cancellations), 2)
                self.assertIs(caught.exception, cancellations[0])
                self.assertIs(caught.exception.__cause__, prior_cause)
                self.assertIs(transport._active, original)
                self.assertIs(transport.pending_owner, owner)
                self.assertEqual(original.not_after, cap)
                self.assertFalse(original.task.done())
                self.assertFalse(transport.retirement_confirmed)
                retained = transport.last_failure
                self.assertIs(retained.primary, native_failure)
                self.assertEqual(retained.cleanup_failures, ())
                self.assertNotIn(cancellations[1], retained.cleanup_failures)
                with self.assertRaises(ExchangeFailed):
                    await transport.retire(owner=object())
                release_close.set()
                await asyncio.wait_for(asyncio.gather(original.task), 0.2)
                with self.assertRaises(ExchangeFailed) as late:
                    await transport.retire(owner=owner)
                self.assertIs(late.exception, retained)
                self.assertIs(transport.last_failure, retained)
                self.assertIs(retained.primary, native_failure)
                self.assertIs(retained.network_failure, native_failure)
                self.assertEqual(retained.cleanup_failures, (close_failure,))
                self.assertIs(caught.exception.__cause__, prior_cause)
            finally:
                release_close.set()
                await asyncio.gather(caller, return_exceptions=True)
                if transport._active is not None:
                    await asyncio.gather(transport._active.task, return_exceptions=True)
