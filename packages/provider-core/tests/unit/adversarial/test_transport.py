"""Failures retain causes and resource ownership without exposing private text."""

import asyncio
import errno
import os
import ssl
import stat
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

import httpx

from provider_core.network import OwnedNetworkBackend
from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.transport import Client, Stream


class TransportFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_limit_charges_offending_chunk_before_writing_that_chunk(self):
        client = Client(Stream([b"abc", b"de"]))
        charged = []
        transport = self.transport()

        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            self.assertRaises(ExchangeFailed),
        ):
            try:
                await transport.exchange_file(
                    httpx.Request("GET", "https://api.example/input"),
                    output_fd=output.fileno(),
                    maximum_bytes=4,
                    account_bytes=charged.append,
                )
            finally:
                self.assertEqual(charged, [3, 2])
                output.seek(0)
                self.assertEqual(output.read(), b"abc")
                self.assertTrue(transport.retirement_confirmed)

    async def test_accounting_refusal_does_not_write_received_chunk(self):
        refusal = RuntimeError("private accounting refusal")

        def refuse(_count):
            raise refusal

        client = Client(Stream([b"abc"]))
        transport = self.transport()

        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange_file(
                    httpx.Request("GET", "https://api.example/input"),
                    output_fd=output.fileno(),
                    maximum_bytes=3,
                    account_bytes=refuse,
                )

            self.assertIs(caught.exception.primary, refusal)
            self.assertEqual(output.tell(), 0)

    async def test_zero_progress_file_write_refuses_without_retrying_or_closing_descriptor(self):
        client = Client(Stream([b"abc"]))
        transport = self.transport()

        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            patch("provider_core.transport.os.write", return_value=0) as write,
        ):
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange_file(
                    httpx.Request("GET", "https://api.example/input"),
                    output_fd=output.fileno(),
                    maximum_bytes=3
                )

            self.assertIsInstance(caught.exception.primary, OSError)
            self.assertEqual(caught.exception.primary.errno, errno.EIO)
            self.assertEqual(write.call_count, 1)
            self.assertTrue(transport.retirement_confirmed)
            os.fstat(output.fileno())

    async def test_cancelled_file_write_stays_owned_until_original_thread_settles(self):
        for (late_failure, cleanup) in (
            (None, None),
            (OSError(errno.ENOSPC, "private disk cause"), None),
            (OSError(errno.ENOSPC, "private disk cause"), OSError(errno.EIO, "private network cleanup")),
        ):
            with self.subTest(late_failure=late_failure is not None, cleanup=cleanup is not None):
                await self._held_file_write(cancel=True, late_failure=late_failure, cleanup=cleanup)

    async def test_expired_file_write_stays_owned_until_original_thread_settles(self):
        await self._held_file_write(cancel=False, late_failure=OSError(errno.ENOSPC, "private disk cause"), cleanup=None)

    async def _held_file_write(self, *, cancel, late_failure, cleanup):
        (entered, release_write) = (asyncio.Event(), threading.Event())
        loop = asyncio.get_running_loop()
        original_write = os.write
        received = []

        class ObservedStream(Stream):
            async def __aiter__(self):
                for chunk in self.chunks:
                    received.append(chunk)
                    yield chunk

        network_closed = asyncio.Event()

        class ObservedClient(Client):
            async def aclose(self):
                try:
                    await super().aclose()
                finally:
                    network_closed.set()

        client = ObservedClient(ObservedStream([b"first", b"never received"]), close_failure=cleanup)

        # silent-defaults: allow-contract -- The long budget lets cancellation win; the short budget exercises exchange expiry.
        transport = HttpTransport(
            trust=ssl.create_default_context(),
            exchange_seconds=2 if cancel else 0.1,
            retirement_seconds=0.03
        )

        owner = object()

        def held_write(descriptor, data):
            loop.call_soon_threadsafe(entered.set)

            if not release_write.wait(timeout=(release_timeout_seconds := 3)):
                raise TimeoutError(f"{release_write.is_set()=} after waiting {release_timeout_seconds=}")

            if not (late_failure is None):
                raise late_failure

            return original_write(descriptor, data)

        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory,
            patch("provider_core.transport.os.write", side_effect=held_write) as write,
        ):
            task = asyncio.create_task(
                transport.exchange_file(
                    httpx.Request("GET", "https://api.example/input"),
                    output_fd=output.fileno(),
                    maximum_bytes=100,
                    owner=owner,
                )
            )

            try:
                await asyncio.wait_for(entered.wait(), 1)

                if cancel:
                    task.cancel()
                    await asyncio.wait_for(network_closed.wait(), 1)
                    task.cancel()

                with self.assertRaises(asyncio.CancelledError if cancel else ExchangeFailed):
                    await task

                self.assertFalse(transport.retirement_confirmed)
                self.assertIs(transport.pending_owner, owner)
                self.assertTrue(client.stream.closed)
                self.assertEqual(write.call_count, 1)
                self.assertEqual(received, [b"first"])
                os.fstat(output.fileno())

                with self.assertRaises(ExchangeFailed):
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=1)

                with self.assertRaises(ExchangeFailed):
                    await transport.retire(owner=object())

                with self.assertRaises(ExchangeFailed):
                    await transport.retire(owner=owner)

                self.assertEqual(factory.call_count, 1)
            finally:
                release_write.set()

                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

                if transport._active is not None:
                    await asyncio.gather(transport._active.task, return_exceptions=True)

                if cleanup is None:
                    await transport.retire(owner=owner)
                else:
                    with self.assertRaises(ExchangeFailed):
                        await transport.retire(owner=owner)

            if late_failure is not None:
                self.assertIs(transport.last_failure.primary, late_failure)
                self.assertIsInstance(transport.last_failure.interruption, asyncio.CancelledError if cancel else TimeoutError)

                if cleanup is not None:
                    self.assertIn(cleanup, transport.last_failure.cleanup_failures)
                else:
                    self.assertTrue(transport.retirement_confirmed)
            else:
                self.assertTrue(transport.retirement_confirmed)
                output.seek(0)
                self.assertEqual(output.read(), b"first")

            self.assertEqual(write.call_count, 1)

        return transport.last_failure

    async def test_invalid_file_descriptor_refuses_before_network_effects(self):
        for (descriptor, error_type) in ((-1, ValueError), (True, TypeError), (None, TypeError), ("private path", TypeError)):
            with (
                self.subTest(kind=type(descriptor).__name__),
                patch("provider_core.transport.httpcore.AsyncConnectionPool") as factory,
            ):
                with self.assertRaises(error_type):
                    await (
                        self
                        .transport()
                        .exchange_file(
                            httpx.Request("GET", "https://api.example/input"),
                            output_fd=descriptor,
                            maximum_bytes=4,
                        )
                    )

                factory.assert_not_called()

    async def test_unusable_file_descriptors_refuse_before_network_owner_creation(self):
        with tempfile.NamedTemporaryFile() as output:
            read_only = os.open(output.name, os.O_RDONLY)
            append_only = os.open(output.name, os.O_WRONLY | os.O_APPEND)
            (read_pipe, write_pipe) = os.pipe()
            closed = os.dup(output.fileno())

            os.close(closed)

            try:
                self.assertTrue(stat.S_ISREG(os.fstat(read_only).st_mode))

                with self.assertRaises(OSError) as caught:
                    os.fstat(closed)

                self.assertEqual(caught.exception.errno, errno.EBADF)

                for descriptor in (closed, read_only, append_only, read_pipe, write_pipe):
                    with (
                        self.subTest(descriptor=descriptor),
                        patch("provider_core.transport.OwnedNetworkBackend", wraps=OwnedNetworkBackend) as network,
                        patch(
                            "provider_core.transport.httpcore.AsyncConnectionPool",
                            return_value=Client(Stream([b"data"]))
                        ) as client,
                    ):
                        transport = self.transport()

                        with self.assertRaises(ExchangeFailed):
                            await transport.exchange_file(
                                httpx.Request("POST", "https://api.example/mutation", content=b"private request"),
                                output_fd=descriptor,
                                maximum_bytes=4,
                            )

                        self.assertTrue(transport.retirement_confirmed)
                        network.assert_not_called()
                        client.assert_not_called()
            finally:
                for descriptor in (read_only, append_only, read_pipe, write_pipe):
                    os.close(descriptor)

    async def test_cancelled_file_inspection_cannot_start_network_work_after_settlement(self):
        await self._held_file_inspection(cancel=True)

    async def test_expired_file_inspection_cannot_start_network_work_after_settlement(self):
        await self._held_file_inspection(cancel=False)

    async def _held_file_inspection(self, *, cancel):
        with tempfile.TemporaryFile() as output:
            (entered, release_inspection) = (asyncio.Event(), threading.Event())
            loop = asyncio.get_running_loop()
            fstat = os.fstat

            def held_inspection(descriptor):
                loop.call_soon_threadsafe(entered.set)

                if not release_inspection.wait(timeout=(release_timeout_seconds := 3)):
                    raise TimeoutError(f"{release_inspection.is_set()=} after waiting {release_timeout_seconds=}")

                return fstat(descriptor)

            # silent-defaults: allow-contract -- The long budget lets cancellation win; the short budget exercises file-output expiry.
            transport = HttpTransport(
                trust=ssl.create_default_context(),
                exchange_seconds=2 if cancel else 0.1,
                retirement_seconds=0.03
            )

            owner = object()

            with (
                patch("provider_core.transport.os.fstat", side_effect=held_inspection) as inspect,
                patch("provider_core.transport.OwnedNetworkBackend", wraps=OwnedNetworkBackend) as network,
                patch("provider_core.transport.httpcore.AsyncConnectionPool") as client,
            ):
                task = asyncio.create_task(
                    transport.exchange_file(
                        httpx.Request("POST", "https://api.example/mutation", content=b"private request"),
                        output_fd=output.fileno(),
                        maximum_bytes=4,
                        owner=owner,
                    )
                )

                try:
                    await asyncio.wait_for(entered.wait(), 1)

                    if cancel:
                        task.cancel()

                    with self.assertRaises(asyncio.CancelledError if cancel else ExchangeFailed):
                        await task

                    self.assertFalse(transport.retirement_confirmed)
                    self.assertIs(transport.pending_owner, owner)
                    self.assertEqual(os.pread(output.fileno(), 1, 0), b"")
                    network.assert_not_called()
                    client.assert_not_called()

                    with self.assertRaises(ExchangeFailed):
                        await transport.retire(owner=owner)
                finally:
                    release_inspection.set()

                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                    if transport._active is not None:
                        await asyncio.gather(transport._active.task, return_exceptions=True)

                    await transport.retire(owner=owner)

                self.assertTrue(transport.retirement_confirmed)
                self.assertEqual(inspect.call_count, 1)
                network.assert_not_called()
                client.assert_not_called()

    async def test_cancellation_before_exchange_task_entry_leaves_borrowed_file_untouched(self):
        create_task = asyncio.create_task

        def cancel_before_entry(coroutine, **kwargs):
            task = create_task(coroutine, **kwargs)

            task.cancel()

            return task

        transport = self.transport()

        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.asyncio.create_task", side_effect=cancel_before_entry),
            patch("provider_core.transport.httpcore.AsyncConnectionPool") as factory,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await transport.exchange_file(
                    httpx.Request("GET", "https://api.example/input"),
                    output_fd=output.fileno(),
                    maximum_bytes=4
                )

            factory.assert_not_called()
            self.assertTrue(transport.retirement_confirmed)
            os.write(output.fileno(), b"still owned")

    async def test_ipv6_proxy_destination_refuses_before_any_network_owner_is_created(self):
        transport = HttpTransport(
            trust=ssl.create_default_context(),
            exchange_seconds=1,
            retirement_seconds=1,
            proxy_socket="/run/proxy.sock"
        )

        with (
            patch("provider_core.transport.OwnedNetworkBackend") as backend,
            patch("provider_core.transport.httpcore.AsyncHTTPProxy") as proxy,
        ):
            with self.assertRaisesRegex(ValueError, "IPv6 literal destinations"):
                await transport.exchange(httpx.Request("GET", "https://[::1]:443/PRIVATE"), maximum_bytes=4)

            backend.assert_not_called()
            proxy.assert_not_called()

        self.assertTrue(transport.retirement_confirmed)

    def _invalid_unix_socket_paths(self):
        return (
            ("", ValueError),
            ("PRIVATE relative", ValueError),
            ("/PRIVATE\x00socket", ValueError),
            ("/" + "x" * 107, ValueError),
            ("/PRIVATE\ud800", ValueError),
            (7, TypeError),
        )

    async def test_invalid_unix_socket_paths_refuse_before_client_creation(self):
        with patch("provider_core.transport.httpcore.AsyncConnectionPool") as factory:
            for (path, error_type) in self._invalid_unix_socket_paths():
                with self.subTest(kind=type(path).__name__), self.assertRaises(error_type):
                    HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1, proxy_socket=path)

        factory.assert_not_called()

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    async def test_invalid_unix_socket_paths_are_not_echoed(self):
        for (path, error_type) in self._invalid_unix_socket_paths():
            with self.subTest(kind=type(path).__name__), self.assertRaises(error_type) as caught:
                HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1, proxy_socket=path)

            self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_received_head_and_read_failure_survive_unconfirmed_cleanup(self):
        release = asyncio.Event()
        primary = OSError("private body failure")

        class HeldCleanup(Client):
            async def aclose(self):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

                await super().aclose()

        headers = [(b"x-request-id", b"request:fixture")]
        client = HeldCleanup(Stream([], failure=primary), headers=headers)
        (transport, owner) = (self.transport(), object())

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            try:
                with self.assertRaises(ExchangeFailed) as caught:
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=7, owner=owner)

                original = caught.exception

                self.assertEqual(original.response_head.headers, tuple(headers))
                self.assertIs(original.primary, primary)
                self.assertIsInstance(original.interruption, TimeoutError)
                self.assertFalse(transport.retirement_confirmed)
                self.assertIs(transport.pending_owner, owner)

                with self.assertRaises(ExchangeFailed):
                    await transport.retire(owner=owner)

                self.assertIs(transport.last_failure, original)
            finally:
                release.set()
                await transport.retire(owner=owner)

        self.assertIs(transport.last_failure, original)
        self.assertTrue(transport.retirement_confirmed)

    async def test_cancelled_body_read_keeps_the_received_head_and_same_cancellation(self):
        entered = asyncio.Event()

        class HeldBody(Stream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        client = Client(HeldBody([]), headers=[(b"x-request-id", b"request:fixture")])
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.1)

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=7))

            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()

                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task

                self.assertIs(transport.last_failure.primary, caught.exception)
                self.assertEqual(transport.last_failure.response_head.headers, ((b"x-request-id", b"request:fixture"),))
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

                await transport.retire()

    async def _failed_bodies(self):
        headers = [(b"x-request-id", b"request:fixture"), (b"private-header", b"private-head-canary")]
        primary = OSError("private body failure")

        for stream in (Stream([b"partial"], failure=primary), Stream([b"too large"])):
            with self.subTest(overflow=stream.failure is None):
                client = Client(stream, status=503, headers=headers)

                with (
                    patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
                    self.assertRaises(ExchangeFailed) as caught,
                ):
                    await self.transport().exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=7)

                failure = caught.exception

                yield failure, headers, client, stream.failure, primary

    async def test_failed_body_keeps_received_head_without_admitting_a_partial_response(self):
        async for (failure, headers, client, stream_failure, primary) in self._failed_bodies():
            self.assertEqual((failure.response_head.status, failure.response_head.headers), (503, tuple(headers)))
            self.assertTrue(client.closed)

            if stream_failure is primary:
                self.assertIs(failure.primary, primary)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    async def test_failed_body_diagnostics_omit_received_headers(self):
        async for (failure, _headers, _client, _stream_failure, _primary) in self._failed_bodies():
            self.assertNotIn("private-head-canary", repr(failure.response_head) + str(failure))

    async def test_foreign_owner_cannot_retire_or_replace_active_exchange(self):
        client = Client(Stream([]), send_gate=asyncio.Event())
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=2, retirement_seconds=0.05)
        (owner, stranger) = (object(), object())
        request = httpx.Request("GET", "https://api.example")

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
            task = asyncio.create_task(transport.exchange(request, maximum_bytes=4, owner=owner))

            try:
                await asyncio.wait_for(client.sent.wait(), 1)

                for other in (None, stranger):
                    with self.assertRaises(ExchangeFailed):
                        await transport.retire(owner=other)

                    with self.assertRaises(ExchangeFailed):
                        await transport.exchange(request, maximum_bytes=4, owner=other)

                    self.assertIs(transport.pending_owner, owner)
                    self.assertFalse(task.done())
                    self.assertFalse(client.closed)

                self.assertEqual(factory.call_count, 1)
            finally:
                task.cancel()

                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertIsNone(transport.pending_owner)
            self.assertTrue(transport.retirement_confirmed)

    def transport(self):
        return HttpTransport(trust=ssl.create_default_context(), exchange_seconds=0.05, retirement_seconds=0.05)

    async def test_overflow_never_hands_off_partial_bytes(self):
        client = Client(Stream([b"abc", b"de"]))

        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            self.assertRaises(ExchangeFailed) as caught,
        ):
            await self.transport().exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

        self.assertIn("4", str(caught.exception))
        self.assertTrue(client.closed)
        self.assertTrue(client.stream.closed)

    async def _read_and_cleanup_failure(self):
        primary = httpx.ReadError("private-read-canary")
        cleanup = RuntimeError("private-close-canary")
        client = Client(Stream([], failure=primary), close_failure=cleanup)
        transport = self.transport()
        request = httpx.Request("GET", "https://api.example/private-url-canary")

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
            try:
                await transport.exchange(request, maximum_bytes=4)
            except ExchangeFailed as failure:
                yield failure, primary, cleanup, transport, factory
            else:
                self.fail("Failed read/close must not yield a response")

    async def test_primary_and_cleanup_causes_are_retained_and_block_reuse(self):
        async for (failure, primary, cleanup, transport, factory) in self._read_and_cleanup_failure():
            self.assertIs(failure.primary, primary)
            self.assertIn(cleanup, failure.cleanup_failures)

            with self.assertRaises(ExchangeFailed):
                await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

            self.assertEqual(factory.call_count, 1)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    async def test_primary_and_cleanup_diagnostics_omit_request_and_causes(self):
        async for (failure, _primary, _cleanup, _transport, _factory) in self._read_and_cleanup_failure():
            rendered = "".join(traceback.format_exception(failure)) + repr(failure)

            for private in ("private-read-canary", "private-close-canary", "private-url-canary"):
                self.assertNotIn(private, rendered)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    async def test_file_write_failure_diagnostics_omit_causes(self):
        primary = OSError(errno.ENOSPC, "private disk cause")
        cleanup = OSError(errno.EIO, "private network cleanup")
        failure = await self._held_file_write(cancel=True, late_failure=primary, cleanup=cleanup)
        rendered = "".join(traceback.format_exception(failure)) + repr(failure)

        self.assertNotIn("private disk cause", rendered)
        self.assertNotIn("private network cleanup", rendered)

    async def test_deadline_and_cancellation_close_before_reuse(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = Client(Stream([]), send_gate=asyncio.Event())
                transport = self.transport()

                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                    task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))

                    await client.sent.wait()

                    if cancel:
                        task.cancel()

                    with self.assertRaises(asyncio.CancelledError if cancel else ExchangeFailed):
                        await task

                    self.assertTrue(client.closed)
                    await transport.retire()

    async def test_overlap_is_rejected_before_another_client_is_created(self):
        client = Client(Stream([]), send_gate=asyncio.Event())
        transport = self.transport()

        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
            task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))

            await client.sent.wait()

            with self.assertRaises(ExchangeFailed):
                await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

            self.assertEqual(factory.call_count, 1)
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_read_failure_survives_cleanup_deadline(self):
        primary = httpx.ReadError("private-read-canary")

        class SlowCloseClient(Client):
            async def aclose(self):
                await asyncio.sleep(1)

        transport = self.transport()

        with (
            patch(
                "provider_core.transport.httpcore.AsyncConnectionPool",
                return_value=SlowCloseClient(Stream([], failure=primary))
            ),
            self.assertRaises(ExchangeFailed) as caught,
        ):
            await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

        self.assertIs(caught.exception.primary, primary)
        self.assertIsInstance(caught.exception.interruption, TimeoutError)

    async def test_cancellation_resistant_send_stays_owned_across_retirement_attempts(self):
        release = asyncio.Event()

        class ResistantClient(Client):
            async def handle_async_request(self, request, **options):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

                return await super().handle_async_request(request, **options)

        client = ResistantClient(Stream([b"ok"]))
        transport = self.transport()

        try:
            with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
                started = asyncio.get_running_loop().time()

                with self.assertRaises(ExchangeFailed) as caught:
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

                self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
                original = caught.exception

                self.assertIsInstance(original.primary, TimeoutError)
                self.assertIsNone(original.response_head)

                with self.assertRaises(ExchangeFailed):
                    await transport.retire()

                self.assertIs(transport.last_failure, original)

                with self.assertRaises(ExchangeFailed):
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)

                self.assertEqual(factory.call_count, 1)
        finally:
            release.set()
            await transport.retire()

        self.assertTrue(client.closed)
        self.assertIs(transport.last_failure, original)
        self.assertEqual(original.response_head.status, 200)
        self.assertEqual(original.response_head.headers, ((b"content-type", b"application/json"),))

    async def test_retirement_before_worker_entry_creates_no_client_and_releases_owner(self):
        transport = self.transport()

        with patch("provider_core.transport.httpcore.AsyncConnectionPool") as factory:
            task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))

            # The exchange is ready first; retirement is ready before its child.
            retirement = asyncio.create_task(transport.retire())

            await retirement

            with self.assertRaises(asyncio.CancelledError):
                await task

            factory.assert_not_called()
            await transport.retire()

    async def test_accounting_observes_chunks_even_when_response_limit_rejects_them(self):
        received = []
        client = Client(Stream([b"abc", b"de"]))

        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            self.assertRaises(ExchangeFailed),
        ):
            await (
                self
                .transport()
                .exchange(
                    httpx.Request("GET", "https://api.example"),
                    maximum_bytes=4,
                    account_bytes=received.append,
                )
            )

        self.assertEqual(received, [3, 2])
        self.assertTrue(client.closed)
