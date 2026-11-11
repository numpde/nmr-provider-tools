"""Default chat factories preserve HTTP grants across one supervised child.

These are real direct-TLS exchanges against a local peer. The unavailable Unix
proxy case proves refusal without direct fallback; successful CONNECT routing
still belongs to the existing Unix-proxy qualification suite.
"""

import asyncio
from contextlib import asynccontextmanager
import gzip
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.chat import ChatEndpoint, ChatEndpointFactory, ChatEndpointSpec
from provider_core.chat_errors import ChatFailed
from provider_core.chat_tls import capture_trust, restore_trust
from provider_core.conversation import ToolFinished
from provider_core.decoded_body import DecodedBodyRejected
from provider_core.diagnostics import failure_evidence
from provider_core.interpreter import Interpreter
from provider_core.transport import ExchangeFailed
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer


def completion():
    return json.dumps({"choices": [{
        "message": call("selected", "prepare", {"format": "text"}),
        "finish_reason": "tool_calls",
    }]}).encode()


@asynccontextmanager
async def launched_children():
    """Observe actual child creation without replacing the process or its I/O."""
    children = []
    launch = asyncio.create_subprocess_exec

    async def observed(*args, **kwargs):
        child = await launch(*args, **kwargs)
        children.append(child)
        return child

    with patch("provider_core.chat_process.asyncio.create_subprocess_exec", side_effect=observed):
        yield children


class ChatProcessContractTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary specimens have bounded direct HTTPS grants; each adversarial case explicitly overrides the relevant grant.
    def factory(self, url, trust, *, request_bytes=32768, response_bytes=8192,
                error_response_bytes=None, proxy_socket=None):
        return ChatEndpointFactory(
            spec=ChatEndpointSpec(configuration_id="fixture", model="fixture-model", url=url,
                                  api_key="PRIVATE_FIXTURE_KEY", reasoning_effort="high"),
            trust=trust, retirement_seconds=0.6, request_bytes=request_bytes,
            response_bytes=response_bytes, error_response_bytes=error_response_bytes,
            proxy_socket=proxy_socket,
        )

    def assert_reaped(self, children, count):
        self.assertEqual(count, len(children))
        self.assertEqual(count, len({child.pid for child in children}))
        self.assertTrue(all(child.returncode is not None for child in children))
        self.assertTrue(all(child.stdin.is_closing() for child in children))

    async def test_factory_preserves_request_identity_trust_and_connected_response_window(self):
        # Allow real DNS and TLS setup, but keep its grant shorter than the
        # response delay so success proves the setup timer was disabled.
        async def reply(_request, _body):
            await asyncio.sleep(1.2)
            return completion()

        async with TlsPeer("success", response_factory=reply) as peer, launched_children() as children:
            url = peer.origin.replace("127.0.0.1", "localhost") + "/configured/chat"
            factory = self.factory(url, peer.trust, error_response_bytes=128)
            endpoint = factory(exchange_seconds=3, connect_seconds=1)
            reference = ChatEndpoint(factory.spec, transport=object(), request_bytes=32768,
                                     response_bytes=8192, error_response_bytes=128)
            self.assertEqual(reference.identity, endpoint.identity)
            self.assertTrue(endpoint.retirement_confirmed)
            self.assertEqual([], children)
            messages = [{"role": "user", "content": "Authored scientific input"}]
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]
            try:
                try:
                    result = await endpoint.complete(messages, tools, not_after=asyncio.get_running_loop().time() + 3)
                except ChatFailed as error:
                    error.add_note(json.dumps(failure_evidence(
                        error, secrets=(factory.spec.api_key, "Authored scientific input"))))
                    raise
                self.assertEqual(json.loads(completion())["choices"][0]["message"], result)
                self.assertEqual(1, len(peer.requests))
                self.assertTrue(peer.requests[0].startswith(b"POST /configured/chat HTTP/1.1\r\n"))
                headers = dict(line.split(b": ", 1) for line in peer.requests[0].split(b"\r\n")[1:] if b": " in line)
                self.assertEqual(b"Bearer PRIVATE_FIXTURE_KEY",
                                 {name.lower(): value for name, value in headers.items()}[b"authorization"])
                self.assertEqual(["localhost"], peer.server_names)
                self.assertEqual({
                    "model": "fixture-model", "messages": messages, "tools": tools,
                    "tool_choice": "required", "parallel_tool_calls": False,
                    "stream": False, "reasoning_effort": "high",
                }, json.loads(peer.request_bodies[0]))
                self.assertEqual(reference.identity, endpoint.identity)
                self.assertTrue(endpoint.retirement_confirmed)
                self.assert_reaped(children, 1)
            finally:
                await endpoint.retire()
            await asyncio.wait_for(peer.closed.wait(), 1)

    async def test_request_limit_refuses_before_any_child_is_started(self):
        async with TlsPeer("success", response_body=completion()) as peer, launched_children() as children:
            factory = self.factory(peer.origin + "/chat", peer.trust, request_bytes=1)
            endpoint = factory(exchange_seconds=3, connect_seconds=1)
            with self.assertRaises(ChatFailed) as caught:
                await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 3)
            self.assertEqual("request_limit", caught.exception.reason)
            self.assertEqual([], children)
            self.assertEqual([], peer.requests)
            await endpoint.retire()
            self.assertTrue(endpoint.retirement_confirmed)

    async def test_expired_original_deadline_refuses_before_child_launch(self):
        async with TlsPeer("success", response_body=completion()) as peer, launched_children() as children:
            endpoint = self.factory(peer.origin + "/chat", peer.trust)(exchange_seconds=3, connect_seconds=1)
            with self.assertRaises(ChatFailed) as caught:
                await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() - 0.01)
            self.assertEqual("exchange_failed", caught.exception.reason)
            self.assertIs(type(caught.exception.cause), ExchangeFailed)
            self.assertIsInstance(caught.exception.cause.primary, TimeoutError)
            self.assertEqual([], children)
            self.assertEqual([], peer.requests)
            await endpoint.retire()
            self.assertTrue(endpoint.retirement_confirmed)

    async def test_success_uses_its_response_limit_and_http_refusal_sends_once(self):
        for status, body, error_bytes in ((200, completion(), 16), (503, b'{"error":{"message":"busy"}}', 64)):
            with self.subTest(status=status):
                async with TlsPeer("success", response_body=body, response_status=status) as peer, launched_children() as children:
                    endpoint = self.factory(peer.origin + "/chat", peer.trust,
                                            error_response_bytes=error_bytes)(exchange_seconds=3, connect_seconds=1)
                    try:
                        if status == 200:
                            await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 3)
                        else:
                            with self.assertRaises(ChatFailed) as caught:
                                await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 3)
                            self.assertEqual("http_status", caught.exception.reason)
                            self.assertEqual(503, caught.exception.response.status)
                        self.assertEqual(1, len(peer.requests))
                        self.assertTrue(endpoint.retirement_confirmed)
                        self.assert_reaped(children, 1)
                    finally:
                        await endpoint.retire()

    async def test_error_and_decompressed_success_caps_are_enforced_in_the_child(self):
        expanded = json.dumps({"content": "x" * 4096}).encode()
        compressed = gzip.compress(expanded)
        self.assertLess(len(compressed), 512)
        cases = (
            (503, b'{"error":{"message":"' + b"x" * 256 + b'"}}', None, 4096, 64),
            (200, compressed, ((b"Content-Encoding", b"gzip"),), 512, 512),
        )
        for status, body, headers, response_bytes, error_bytes in cases:
            with self.subTest(status=status):
                async with TlsPeer("success", response_status=status, response_body=body,
                                   response_headers=headers) as peer, launched_children() as children:
                    endpoint = self.factory(peer.origin + "/chat", peer.trust, response_bytes=response_bytes,
                                            error_response_bytes=error_bytes)(exchange_seconds=3, connect_seconds=1)
                    try:
                        with self.assertRaises(ChatFailed) as caught:
                            await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 3)
                        self.assertEqual("exchange_failed", caught.exception.reason)
                        self.assertIs(type(caught.exception.cause.primary), DecodedBodyRejected)
                        self.assertEqual(1, len(peer.requests))
                        self.assertTrue(endpoint.retirement_confirmed)
                        self.assert_reaped(children, 1)
                    finally:
                        await endpoint.retire()

    async def test_unavailable_fixed_unix_proxy_never_falls_back_to_direct_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            proxy = str(Path(directory) / "absent-proxy.sock")
            async with TlsPeer("success", response_body=completion()) as peer, launched_children() as children:
                endpoint = self.factory(peer.origin + "/chat", peer.trust,
                                        proxy_socket=proxy)(exchange_seconds=3, connect_seconds=1)
                try:
                    with self.assertRaises(ChatFailed) as caught:
                        await endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 3)
                    self.assertEqual("exchange_failed", caught.exception.reason)
                    observation = caught.exception.cause.network_io
                    self.assertEqual(proxy, observation.configured_unix_path)
                    self.assertEqual(proxy, observation.unix_path)
                    self.assertEqual("connect_unix", observation.stage)
                    self.assertEqual([], peer.requests)
                    self.assertEqual([], peer.server_names)
                    self.assertTrue(endpoint.retirement_confirmed)
                    self.assert_reaped(children, 1)
                finally:
                    await endpoint.retire()

    async def test_caller_cancellation_preserves_its_original_failure_and_reaps_the_child(self):
        async with TlsPeer("headers-stall") as peer, launched_children() as children:
            endpoint = self.factory(peer.origin + "/chat", peer.trust)(exchange_seconds=5, connect_seconds=1)
            running = asyncio.create_task(endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 5))
            try:
                await asyncio.wait_for(peer.received.wait(), 3)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(running, 2)
                diagnostic = caught.exception.__cause__
                self.assertIs(type(diagnostic), ChatFailed)
                self.assertEqual("request_cancelled", diagnostic.reason)
                self.assertIs(diagnostic.cause, endpoint._transport.last_failure)
                self.assertIs(diagnostic.cause.caller_interruption, caught.exception)
                self.assertTrue(endpoint.retirement_confirmed)
                self.assertEqual(1, len(peer.requests))
                self.assert_reaped(children, 1)
                await asyncio.wait_for(peer.closed.wait(), 1)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                await endpoint.retire()

    async def test_cancellation_after_reply_eof_preserves_the_original_exit_observer(self):
        actual_exit, reply_eof = asyncio.Event(), asyncio.Event()
        allow_observation, observation_cancelled = asyncio.Event(), asyncio.Event()
        children = []
        trust_directories = []
        launch = asyncio.create_subprocess_exec
        temporary_directory = tempfile.TemporaryDirectory

        def observe_directory(*args, **kwargs):
            directory = temporary_directory(*args, **kwargs)
            trust_directories.append(Path(directory.name))
            return directory

        async def observe_exit(*args, **kwargs):
            child = await launch(*args, **kwargs)
            children.append(child)
            original_wait, original_read = child.wait, child.stdout.read

            async def held_wait():
                returncode = await original_wait()
                actual_exit.set()
                try:
                    await allow_observation.wait()
                except asyncio.CancelledError:
                    observation_cancelled.set()
                    raise
                return returncode

            async def observed_read(size):
                value = await original_read(size)
                if size == 1 and not value:
                    reply_eof.set()
                return value

            child.wait, child.stdout.read = held_wait, observed_read
            return child

        async with TlsPeer("success", response_body=completion()) as peer:
            endpoint = self.factory(peer.origin + "/chat", peer.trust)(exchange_seconds=5, connect_seconds=1)
            with patch("provider_core.chat_process.asyncio.create_subprocess_exec", side_effect=observe_exit), \
                    patch("provider_core.chat_process.tempfile.TemporaryDirectory", side_effect=observe_directory):
                running = asyncio.create_task(endpoint.complete([], [], not_after=asyncio.get_running_loop().time() + 5))
                try:
                    await asyncio.wait_for(asyncio.gather(actual_exit.wait(), reply_eof.wait()), 3)
                    self.assertTrue(all(child.returncode == 0 for child in children))
                    self.assertEqual(1, len(trust_directories))
                    self.assertTrue(trust_directories[0].is_dir())
                    running.cancel()
                    await asyncio.wait({running}, timeout=0.05)
                    self.assertFalse(observation_cancelled.is_set())
                    self.assertTrue(trust_directories[0].is_dir())
                    allow_observation.set()
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await asyncio.wait_for(running, 2)
                    diagnostic = caught.exception.__cause__
                    self.assertEqual("request_cancelled", diagnostic.reason)
                    self.assertIs(diagnostic.cause.caller_interruption, caught.exception)
                    self.assertFalse(observation_cancelled.is_set())
                    self.assertTrue(endpoint.retirement_confirmed)
                    self.assert_reaped(children, 1)
                    self.assertFalse(trust_directories[0].exists())
                finally:
                    allow_observation.set()
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    await endpoint.retire()

    async def test_interpreter_keeps_its_existing_retry_budget_and_dispatches_once(self):
        for first_reply in ("unavailable", "lost"):
            with self.subTest(first_reply=first_reply):
                replies = 0

                async def response(_request, _body):
                    nonlocal replies
                    replies += 1
                    if replies == 1:
                        if first_reply == "lost":
                            return None
                        peer.response_status = 503
                        return b'{"error":{"message":"temporarily unavailable"}}'
                    peer.response_status = 200
                    return completion()

                async with TlsPeer("success", response_factory=response) as peer, launched_children() as children:
                    owner = Interpreter((self.factory(peer.origin + "/chat", peer.trust),),
                                        timeout_seconds=8, context_bytes=16384, secrets=("PRIVATE_FIXTURE_KEY",))
                    dispatch = AsyncMock(return_value=ToolFinished("prepared"))
                    tools = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]
                    try:
                        result = await owner.run([], tools, dispatch)
                        self.assertEqual("prepared", result.value)
                        self.assertEqual(2, len(peer.requests))
                        dispatch.assert_awaited_once()
                        self.assertTrue(owner.retirement_confirmed)
                        self.assert_reaped(children, 2)
                    finally:
                        await owner.retire()

    async def test_captured_tls_settings_round_trip_without_replacing_explicit_policy(self):
        async with TlsPeer("success", response_body=completion()) as peer:
            trust = peer.trust
            trust.minimum_version = ssl.TLSVersion.TLSv1_2
            trust.maximum_version = ssl.TLSVersion.TLSv1_2
            trust.hostname_checks_common_name = False
            trust.verify_flags |= ssl.VERIFY_X509_TRUSTED_FIRST
            restored = restore_trust(capture_trust(trust))
            self.assertIsInstance(restored, ssl.SSLContext)
            for field in ("minimum_version", "maximum_version", "hostname_checks_common_name",
                          "verify_flags", "verify_mode", "check_hostname", "options"):
                self.assertEqual(getattr(trust, field), getattr(restored, field), msg=field)
            self.assertEqual(trust.get_ciphers(), restored.get_ciphers())

    def test_opaque_tls_context_is_refused_without_silently_changing_trust(self):
        with self.assertRaises(ValueError):
            capture_trust(ssl.create_default_context())
