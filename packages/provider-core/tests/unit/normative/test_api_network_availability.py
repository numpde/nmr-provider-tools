"""Actual network ownership separates availability from identical local failures."""

import asyncio
import errno
import socket
import ssl
import unittest
from unittest.mock import patch

import anyio
import httpcore
import httpx

from provider_core.api import ApiFailure
from provider_core.failure_projection import owned_relationships
from provider_core.network import OwnedNetworkBackend, network_availability
from provider_core.transport import ExchangeFailed, HttpTransport, temporary_io_failure
from tests.fakes.api import api, response_headers
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.uploads import UPLOAD_REF


class RawStream:
    # silent-defaults: allow-contract -- An empty controlled stream models immediate EOF; individual witnesses supply bytes or faults.
    def __init__(self, *, chunks=(), error=None, stall=None, close_gate=None, close_error=None):
        self.chunks = list(chunks)
        self.error = error
        self.stall = stall
        self.close_gate = close_gate
        self.close_error = close_error
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        self.entered.set()
        if self.chunks:
            return self.chunks.pop(0)
        if self.stall is not None:
            await self.stall.wait()
        if self.error is not None:
            raise self.error
        return b""

    async def write(self, buffer, timeout=None):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None

    async def aclose(self):
        self.closing.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class Backend:
    def __init__(self, stream=None, *, error=None):
        self.stream = stream
        self.error = error

    async def connect_tcp(self, **options):
        if self.error is not None:
            raise self.error
        return self.stream

    async def connect_unix_socket(self, **options):
        return await self.connect_tcp(**options)


# silent-defaults: allow-contract -- ConnectError is the controlled outer wrapper; order witnesses explicitly select ReadError.
def wrapper(error, kind=httpcore.ConnectError):
    wrapped = kind("controlled network wrapper")
    wrapped.__cause__ = error
    return wrapped


class ApiNetworkAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, **changes):
        return HttpTransport(**(dict(trust=ssl.create_default_context(), exchange_seconds=.2, retirement_seconds=.1) | changes))

    async def capture_connect(self, error, *, proxy_socket=None, path=None):
        network = OwnedNetworkBackend(proxy_socket=proxy_socket)
        network._backend = Backend(error=error)
        with self.assertRaises(BaseException) as caught:
            if path is None:
                await network.connect_tcp("api.fixture.example", 443)
            else:
                await network.connect_unix_socket(path)
        self.assertIs(caught.exception, error)
        return network.observation

    # silent-defaults: allow-contract -- Ordinary reads have neither effect flag; the operation matrix explicitly supplies both flags.
    async def failed_exchange(self, client, method, path, *, issuance=False, mutation=False):
        with self.assertRaises(ApiFailure) as caught:
            await client._exchange("controlled availability", method=method, path=path, query=b"",
                                   maximum_bytes=4096, issuance=issuance, attempt_mutation=mutation)
        return caught.exception

    async def test_causal_availability_has_no_wrapper_order_or_dns_errno_prediction(self):
        positives = [socket.gaierror(code, "resolver failure") for code in (socket.EAI_AGAIN, socket.EAI_NONAME, socket.EAI_FAIL)]
        positives += [ConnectionResetError(errno.ECONNRESET, "reset"), TimeoutError(),
                      ssl.SSLEOFError(), ssl.SSLZeroReturnError(), anyio.EndOfStream(), anyio.BrokenResourceError()]
        positives += [wrapper(ExceptionGroup("all addresses", [ConnectionRefusedError(), TimeoutError()])),
                      wrapper(wrapper(socket.gaierror(socket.EAI_NONAME, "resolver"), httpcore.ReadError))]
        negatives = [RuntimeError(), ValueError(), PermissionError(errno.EACCES, "denied"),
                     ssl.SSLCertVerificationError(), ssl.CertificateError(), ssl.SSLError(1, "hard TLS failure"),
                     httpcore.RemoteProtocolError(), httpcore.LocalProtocolError(), httpcore.PoolTimeout(),
                     ExceptionGroup("mixed", [ConnectionRefusedError(), RuntimeError()]),
                     ExceptionGroup("mixed TLS", [TimeoutError(), ssl.SSLCertVerificationError()])]
        for original, expected in [(item, True) for item in positives] + [(item, False) for item in negatives]:
            with self.subTest(error=type(original).__name__, expected=expected):
                failure = wrapper(original)
                facts = await self.capture_connect(failure)
                # The pinned pool can erase links on this same original object.
                failure.__cause__ = None
                failure.__context__ = RuntimeError("unrelated later handler")
                self.assertIs(facts.cause, original)
                self.assertIs(network_availability(failure, facts, network_failure=failure), expected)
        for incidental in (RuntimeError("local"), OSError("local setup")):
            incidental.__context__ = ConnectionRefusedError()
            facts = await self.capture_connect(incidental)
            self.assertFalse(network_availability(incidental, facts, network_failure=incidental))
        self.assertFalse(temporary_io_failure(socket.gaierror(socket.EAI_NONAME, "unchanged generic policy"), 8))

    async def test_only_the_configured_unix_connect_can_admit_absent_socket(self):
        for configured, actual, error, expected in (
            ("/tmp/proxy.sock", "/tmp/proxy.sock", FileNotFoundError(errno.ENOENT, "absent"), True),
            (None, "/tmp/proxy.sock", FileNotFoundError(errno.ENOENT, "absent"), False),
            ("/tmp/proxy.sock", "/tmp/other.sock", FileNotFoundError(errno.ENOENT, "absent"), False),
            ("/tmp/proxy.sock", "/tmp/proxy.sock", PermissionError(errno.EACCES, "denied"), False),
        ):
            with self.subTest(configured=configured, actual=actual, errno=error.errno):
                failure = wrapper(error)
                facts = await self.capture_connect(failure, proxy_socket=configured, path=actual)
                self.assertIs(network_availability(failure, facts, network_failure=failure), expected)
        failure = wrapper(FileNotFoundError(errno.ENOENT, "local missing file"))
        facts = await self.capture_connect(failure)
        self.assertFalse(network_availability(failure, facts, network_failure=failure))

    async def test_each_api_contract_keeps_its_current_exact_route_and_effects(self):
        grant_path = f"/provider/v1/jobs/job:example/uploads/{UPLOAD_REF}/read-capability"
        progress_path = f"/provider/v1/execution-attempts/{ATTEMPT_REF}/progress"
        cases = [
            ("GET", "/provider/v1/jobs/job:example/uploads", "read", False, False),
            ("POST", "/provider/v1/hello", "hello", False, False),
            ("POST", "/provider/v1/execution-attempts/start", "start", False, True),
            ("PUT", progress_path, "progress", False, True),
            ("POST", "/provider/v1/execution-attempts/complete", "terminal", False, True),
            ("POST", "/provider/v1/execution-attempts/fail", "terminal", False, True),
            ("POST", grant_path, "grant", True, False),
            ("POST", "/provider/v1/unreviewed", None, False, False),
        ]
        for method, path, selected, issuance, mutation in cases:
            with self.subTest(method=method, path=path):
                original = wrapper(socket.gaierror(socket.EAI_NONAME, "original resolver"))
                transport = self.transport()
                client = api(transport, "https://api.fixture.example")
                with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(error=original)):
                    failure = await self.failed_exchange(client, method, path, issuance=issuance, mutation=mutation)
                checks = dict(read=client.can_retry_read, hello=client.can_refresh_hello, start=client.can_replay_start,
                              terminal=client.can_replay_terminal,
                              progress=lambda error: client.can_defer_phase_progress(error, execution_attempt_ref=ATTEMPT_REF),
                              grant=lambda error: client.can_renew_read_grant(error, job_ref="job:example", upload_ref=UPLOAD_REF))
                for name, check in checks.items():
                    self.assertIs(check(failure), name == selected)
                    self.assertFalse(check(ApiFailure("foreign", "failure")))
                self.assertIs(client.can_retry_condition(failure, execution_attempt_ref=ATTEMPT_REF), selected == "progress")
                self.assertIs(failure.issuance_may_have_committed, issuance)
                self.assertIs(failure.attempt_mutation_may_have_committed, mutation)
                # Same known availability cannot authorize foreign cleanup or a later operation.
                failure.cause.cleanup_failures = (OSError("independent close"),)
                self.assertFalse(any(check(failure) for check in checks.values()))
                failure.cause.cleanup_failures = ()
                client.last_failure = ApiFailure("next", "failure")
                self.assertFalse(any(check(failure) for check in checks.values()))

    async def test_same_read_timeout_from_actual_stream_and_local_account_have_distinct_permission(self):
        original = httpcore.ReadTimeout("same exact object")
        head = b"HTTP/1.1 200 OK\r\n" + b"".join(k+b": "+v+b"\r\n" for k,v in response_headers()) + b"Content-Length: 2\r\n\r\nx"
        for local in (False, True):
            with self.subTest(local=local):
                # silent-defaults: allow-contract -- The local specimen supplies a complete body so native reads cannot author its fault.
                chunks = [head+b"x"] if local else [head]
                # silent-defaults: allow-contract -- Only the native specimen raises from the stream; both use the same original exception.
                native_error = None if local else original
                stream = RawStream(chunks=chunks, error=native_error)
                transport = self.transport()
                client = api(transport, "https://api.fixture.example")
                exchange = transport.exchange
                def account(count):
                    raise original
                async def accounted(request, **options):
                    # silent-defaults: allow-contract -- Only the local-fault specimen installs accounting that raises the original object.
                    return await exchange(request, **options, account_bytes=account if local else None)
                with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(stream)), patch.object(transport, "exchange", side_effect=accounted):
                    failure = await self.failed_exchange(client, "GET", "/provider/v1/jobs/job:example/uploads")
                self.assertIs(failure.cause.primary, original)
                self.assertIs(client.can_retry_read(failure), not local)
                self.assertTrue(transport.retirement_confirmed)
                self.assertTrue(stream.closed)

    async def test_eof_requires_nonzero_actual_read_and_cannot_author_local_protocol_fault(self):
        for maximum, expected in ((0, False), (10, True)):
            network = OwnedNetworkBackend()
            stream = network._own(RawStream())
            self.assertEqual(await stream.read(maximum), b"")
            failure = httpcore.RemoteProtocolError("controlled truncation")
            self.assertIs(network_availability(failure, network.observation, network_failure=failure), expected)
            self.assertFalse(network_availability(RuntimeError(), network.observation, network_failure=failure))
            await network.aclose()

    async def test_sdk_deadline_pending_io_qualifies_but_caller_cancel_remains_exact_and_blocks(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                gate = asyncio.Event()
                stream = RawStream(stall=asyncio.Event(), close_gate=gate)
                transport = self.transport(exchange_seconds=.02, retirement_seconds=.2)
                client = api(transport, "https://api.fixture.example")
                with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(stream)):
                    task = asyncio.create_task(client._exchange("read", method="GET", path="/provider/v1/jobs/job:example/uploads", query=b"", maximum_bytes=4096))
                    await asyncio.wait_for(stream.closing.wait(), 1)
                    if cancel:
                        task.cancel("original caller")
                        await asyncio.sleep(0)
                    gate.set()
                    try:
                        await task
                    except asyncio.CancelledError as observed:
                        self.assertTrue(cancel)
                        self.assertIs(client.last_failure.cause.caller_interruption, observed)
                        self.assertIn(("transport caller interruption", observed), list(owned_relationships(client.last_failure.cause)))
                    except ApiFailure:
                        self.assertFalse(cancel)
                failure = client.last_failure
                self.assertTrue(transport.retirement_confirmed)
                self.assertTrue(failure.cause.network_io.deadline_cancelled)
                self.assertIs(client.can_retry_read(failure), not cancel)

    async def test_head_and_protocol_guards_survive_real_stream_eof(self):
        grant_path = f"/provider/v1/jobs/job:example/uploads/{UPLOAD_REF}/read-capability"
        for status, topology, malformed, read_allowed in (
            (200, b"dev-local", False, True), (200, b"wrong", False, False),
            (200, b"dev-local", True, False), (500, b"dev-local", False, True),
            (429, b"dev-local", False, True),
        ):
            for method, path in (("GET", "/provider/v1/jobs/job:example/uploads"), ("POST", grant_path)):
                with self.subTest(status=status, topology=topology, malformed=malformed, method=method):
                    headers = response_headers(status=status, topology=topology)
                    # silent-defaults: allow-contract -- The matrix deliberately contrasts malformed chunk syntax with actual truncated-body EOF.
                    framing = b"Transfer-Encoding: chunked\r\n\r\nnothex\r\nx" if malformed else b"Content-Length: 2\r\n\r\nx"
                    stream = RawStream(chunks=[f"HTTP/1.1 {status} controlled\r\n".encode() + b"".join(k+b": "+v+b"\r\n" for k,v in headers) + framing])
                    transport = self.transport()
                    client = api(transport, "https://api.fixture.example")
                    with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(stream)):
                        failure = await self.failed_exchange(client, method, path, issuance=method == "POST")
                    self.assertTrue(transport.retirement_confirmed)
                    self.assertIs(type(failure.cause.primary), httpcore.RemoteProtocolError)
                    self.assertIs(failure.cause.network_io.eof, not malformed)
                    if method == "GET":
                        self.assertIs(client.can_retry_read(failure), read_allowed)
                    else:
                        self.assertIs(client.can_renew_read_grant(failure, job_ref="job:example", upload_ref=UPLOAD_REF), read_allowed and status == 200)
                        self.assertTrue(failure.issuance_may_have_committed)

    async def test_actual_close_failure_blocks_availability_and_retains_independent_error(self):
        outage = httpcore.ReadTimeout("original outage")
        cleanup = RuntimeError("original close failure")
        stream = RawStream(error=outage, close_error=cleanup)
        transport = self.transport()
        client = api(transport, "https://api.fixture.example")
        with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(stream)):
            failure = await self.failed_exchange(client, "GET", "/provider/v1/jobs/job:example/uploads")
        self.assertFalse(transport.retirement_confirmed)
        self.assertIs(failure.cause.network_io.error, outage)
        self.assertIs(failure.cause.primary, cleanup)
        self.assertIn(cleanup, failure.cause.cleanup_failures)
        self.assertFalse(client.can_retry_read(failure))
        with self.assertRaises(ExchangeFailed) as retained:
            await client.retire()
        self.assertIs(retained.exception, failure.cause)
        self.assertIs(client.last_failure, failure)
        self.assertFalse(transport.retirement_confirmed)
        self.assertIn(cleanup, failure.cause.cleanup_failures)
        self.assertFalse(client.can_retry_read(failure))

    async def test_typed_io_preserves_explicit_mixed_causes_and_exact_connection_family(self):
        for kind in (httpcore.ReadTimeout, httpcore.ConnectTimeout, TimeoutError, ssl.SSLEOFError,
                     socket.gaierror, ConnectionError):
            for leaf, expected in ((ConnectionResetError(), True),
                                   (ExceptionGroup("all available", [TimeoutError(), ConnectionRefusedError()]), True),
                                   (ExceptionGroup("mixed local", [TimeoutError(), RuntimeError()]), False),
                                   (ExceptionGroup("mixed TLS", [TimeoutError(), ssl.SSLCertVerificationError()]), False)):
                with self.subTest(kind=kind.__name__, leaf=type(leaf).__name__, expected=expected):
                    original = kind()
                    original.__cause__ = leaf
                    facts = await self.capture_connect(original)
                    self.assertIs(network_availability(original, facts, network_failure=original), expected)
        for kind, expected in ((httpcore.ReadTimeout, True), (TimeoutError, True),
                               (httpcore.ConnectError, False), (ConnectionError, False), (ssl.SSLEOFError, False)):
            with self.subTest(kind=kind.__name__, implementation_cancel=True):
                original = kind()
                original.__cause__ = asyncio.CancelledError()
                facts = await self.capture_connect(original)
                self.assertIs(network_availability(original, facts, network_failure=original), expected)
        class CustomConnection(ConnectionError):
            pass
        for original, expected in ((ConnectionError(), True), (CustomConnection(), False)):
            facts = await self.capture_connect(original)
            self.assertIs(network_availability(original, facts, network_failure=original), expected)

    async def test_sdk_deadline_preserves_explicit_native_cancellation_cause(self):
        for cause, expected in (
            (None, True), (ConnectionResetError(), True),
            (ssl.SSLCertVerificationError(), False), (RuntimeError(), False),
            (ExceptionGroup("mixed", [TimeoutError(), RuntimeError()]), False),
        ):
            with self.subTest(cause=type(cause).__name__):
                cancellations = []
                class CancelledRead(RawStream):
                    async def read(self, max_bytes, timeout=None):
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError as cancellation:
                            cancellations.append(cancellation)
                            raise cancellation from cause
                stream = CancelledRead()
                transport = self.transport(exchange_seconds=.02)
                client = api(transport, "https://api.fixture.example")
                with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(stream)):
                    failure = await self.failed_exchange(client, "GET", "/provider/v1/jobs/job:example/uploads")
                observation = failure.cause.network_io
                self.assertTrue(transport.retirement_confirmed)
                self.assertIs(observation.error, cancellations[0])
                self.assertIs(observation.cause, cause)
                self.assertTrue(observation.deadline_cancelled)
                self.assertIsNone(failure.cause.caller_interruption)
                self.assertIs(client.can_retry_read(failure), expected)
