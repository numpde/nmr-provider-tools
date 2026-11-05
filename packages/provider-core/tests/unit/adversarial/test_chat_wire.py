"""Moving HTTP evidence across a process cannot grant new retry permission."""

import asyncio
import errno
import io
import json
import ssl
import struct
import unittest

import httpcore

from provider_core.chat_errors import ChatFailed
from provider_core.chat_wire import (
    failure_message,
    framed,
    message_bytes,
    read_message,
    read_reply,
    read_request,
    response_message,
)
from provider_core.decoded_body import DecodedBodyRejected
from provider_core.interpreter import _retryable
from provider_core.network import NetworkIOObservation
from provider_core.response_json import ResponseRejected
from provider_core.transport import HttpResponse
from provider_core.transport_errors import ExchangeFailed


def transferred(failure):
    document = json.loads(message_bytes(failure_message(failure)))
    return read_reply(document, maximum_bytes=32, error_maximum_bytes=16)


def retry_allowed(failure):
    return _retryable(ChatFailed("exchange_failed", "The model request failed.", cause=failure))


# silent-defaults: allow-contract -- These fixtures model a response-read failure unless a case names another original I/O stage.
def observed_failure(primary, *, stage="read", **facts):
    return ExchangeFailed(
        "The original HTTP exchange failed.", primary=primary, network_failure=primary,
        network_io=NetworkIOObservation(stage=stage, error=primary,
                                        cause=primary.__cause__, context=primary.__context__), **facts,
    )


class ChatWireAvailabilityTests(unittest.TestCase):
    def test_shared_deadline_evidence_keeps_identity_required_for_retry(self):
        for shared in (True, False):
            with self.subTest(shared_original_cancellation=shared):
                cancellation = asyncio.CancelledError("same spelling does not establish ownership")
                native = cancellation if shared else asyncio.CancelledError(str(cancellation))
                original = ExchangeFailed(
                    "Original absolute deadline expired.", primary=TimeoutError("original deadline"),
                    network_failure=native,
                    network_io=NetworkIOObservation(stage="read", error=cancellation, deadline_cancelled=True),
                )
                self.assertEqual(shared, retry_allowed(original))
                received = transferred(original)
                self.assertEqual(shared, received.network_failure is received.network_io.error)
                self.assertEqual(shared, retry_allowed(received))

    def test_independent_cleanup_or_caller_interruption_still_prohibits_retry(self):
        for obligation in ("cleanup", "caller"):
            with self.subTest(obligation=obligation):
                cleanup = OSError(errno.EIO, "independent original close failure")
                cancellation = asyncio.CancelledError("original caller interruption")
                # silent-defaults: allow-contract -- Each subcase injects only its named independent obligation.
                cleanup_failures = (cleanup,) if obligation == "cleanup" else ()
                # silent-defaults: allow-contract -- The cleanup-only subcase has no caller cancellation.
                caller_interruption = cancellation if obligation == "caller" else None
                original = observed_failure(
                    httpcore.ReadTimeout("response read timed out"),
                    cleanup_failures=cleanup_failures,
                    caller_interruption=caller_interruption,
                    proxy_connect_status=503,
                )
                self.assertFalse(retry_allowed(original))
                received = transferred(original)
                self.assertFalse(retry_allowed(received))
                if obligation == "cleanup":
                    self.assertEqual(errno.EIO, received.cleanup_failures[0].errno)
                else:
                    self.assertIs(type(received.caller_interruption), asyncio.CancelledError)

    def test_tls_protocol_and_local_errors_do_not_become_availability(self):
        failures = (
            ssl.SSLCertVerificationError(1, "the configured trust did not verify the peer"),
            ssl.SSLError(1, "TLS protocol failure"),
            httpcore.LocalProtocolError("invalid request framing"),
            PermissionError(errno.EACCES, "local permission refusal"),
            RuntimeError("looks like a retryable httpcore.ReadTimeout"),
        )
        for primary in failures:
            with self.subTest(kind=type(primary).__name__):
                original = observed_failure(primary)
                self.assertFalse(retry_allowed(original))
                self.assertFalse(retry_allowed(transferred(original)))

    def test_actual_eof_and_proxy_status_preserve_existing_classification(self):
        primary = httpcore.RemoteProtocolError("peer disconnected without response headers")
        eof = ExchangeFailed(
            "Reply lost after dispatch.", primary=primary, network_failure=primary,
            network_io=NetworkIOObservation(stage="read", eof=True),
        )
        without_eof = observed_failure(httpcore.RemoteProtocolError(str(primary)))
        for expected, original in (
            (True, eof), (False, without_eof),
            (True, observed_failure(httpcore.ConnectError("proxy refusal"), proxy_connect_status=503)),
            (False, observed_failure(httpcore.ConnectError("proxy refusal"), proxy_connect_status=407)),
        ):
            with self.subTest(expected_retry=expected, proxy_status=original.proxy_connect_status):
                self.assertEqual(expected, retry_allowed(original))
                received = transferred(original)
                self.assertIs(received.primary, received.network_failure)
                self.assertEqual(expected, retry_allowed(received))

    def test_unix_refusal_remains_bound_to_its_original_configured_path(self):
        for path in ("/private/model.sock", "/private/other.sock"):
            with self.subTest(path=path):
                primary = FileNotFoundError(errno.ENOENT, "configured proxy is absent")
                original = ExchangeFailed(
                    "Cannot connect to the selected proxy.", primary=primary, network_failure=primary,
                    network_io=NetworkIOObservation(stage="connect_unix", error=primary, unix_path=path,
                                                    configured_unix_path="/private/model.sock"),
                )
                expected = path == "/private/model.sock"
                self.assertEqual(expected, retry_allowed(original))
                self.assertEqual(expected, retry_allowed(transferred(original)))

    def test_one_nonavailability_group_member_prevents_retry(self):
        for last, expected in ((ConnectionResetError(errno.ECONNRESET, "reset"), True),
                               (ssl.SSLCertVerificationError(1, "untrusted certificate"), False)):
            with self.subTest(expected_retry=expected):
                primary = httpcore.ConnectError("both address attempts failed")
                primary.__cause__ = ExceptionGroup("original connection attempts", (
                    ConnectionRefusedError(errno.ECONNREFUSED, "refused"), last,
                ))
                original = observed_failure(primary, stage="connect_tcp")
                self.assertEqual(expected, retry_allowed(original))
                received = transferred(original)
                self.assertIs(received.primary.__cause__, received.network_io.cause)
                self.assertEqual(expected, retry_allowed(received))

    def test_cyclic_cause_evidence_does_not_become_a_retryable_leaf(self):
        primary, related = httpcore.ReadError("read"), httpcore.ConnectError("connect")
        primary.__cause__, related.__cause__ = related, primary
        original = observed_failure(primary)
        self.assertFalse(retry_allowed(original))
        received = transferred(original)
        self.assertIs(received.primary.__cause__.__cause__, received.primary)
        self.assertFalse(retry_allowed(received))

    def test_decoded_body_refusals_keep_their_closed_reason_and_no_retry(self):
        for reason in ("size_limit", "invalid_encoding", "unsupported_encoding"):
            with self.subTest(reason=reason):
                original = observed_failure(DecodedBodyRejected(reason))
                self.assertFalse(retry_allowed(original))
                received = transferred(original)
                self.assertIs(type(received.primary), DecodedBodyRejected)
                self.assertEqual(reason, received.primary.reason)
                self.assertFalse(retry_allowed(received))

    def test_malformed_cleanup_cannot_be_read_as_no_retirement_obligation(self):
        original = observed_failure(httpcore.ReadTimeout("response read timed out"))
        for cleanup in ("", {}, [None], [True]):
            with self.subTest(cleanup=cleanup):
                document = failure_message(original)
                document["cleanup"] = cleanup
                with self.assertRaises((ValueError, TypeError)):
                    read_reply(document, maximum_bytes=32, error_maximum_bytes=16)

    def test_boolean_or_float_version_is_not_the_protocol_version(self):
        for version in (True, 1.0):
            with self.subTest(version=version):
                document = response_message(HttpResponse(200, (), b"{}"))
                document["version"] = version
                with self.assertRaises(ValueError):
                    read_reply(document, maximum_bytes=32, error_maximum_bytes=16)


class ChatWireFramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_length_is_rejected_before_waiting_for_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 1025))
        # No body and no EOF: a parser that starts reading the unadmitted body
        # would wait until this independent test watchdog expires.
        async with asyncio.timeout(0.2):
            with self.assertRaises(ValueError):
                await read_message(reader, 1024)

    async def test_partial_reply_cannot_produce_a_response(self):
        raw = message_bytes(response_message(HttpResponse(200, (), b'{"choices":[]}')))
        wire = framed(raw)
        for partial in (wire[:2], wire[:-1]):
            with self.subTest(received_bytes=len(partial)):
                reader = asyncio.StreamReader()
                reader.feed_data(partial)
                reader.feed_eof()
                with self.assertRaises(asyncio.IncompleteReadError):
                    document = await read_message(reader, 4096)
                    read_reply(document, maximum_bytes=32, error_maximum_bytes=16)

    async def test_duplicate_outcome_field_is_not_a_complete_reply(self):
        raw = b'{"version":1,"kind":"failure","kind":"response","status":200,"headers":[],"body":""}'
        reader = asyncio.StreamReader()
        reader.feed_data(framed(raw))
        reader.feed_eof()
        with self.assertRaises(ResponseRejected):
            await read_message(reader, 4096)

    def test_request_requires_one_complete_frame_and_no_trailing_bytes(self):
        wire = framed(message_bytes({"version": 1, "kind": "request"}))
        for raw in (wire[:-1], wire + b"x", wire + wire):
            with self.subTest(received_bytes=len(raw)):
                with self.assertRaises(ValueError):
                    read_request(io.BytesIO(raw), 4096)
