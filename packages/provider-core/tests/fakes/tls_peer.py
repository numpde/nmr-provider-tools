"""Finite TLS peer; explicit bind address, disposable keys and certificates."""

import asyncio
from contextlib import contextmanager
import ipaddress
import logging
import ssl
import tempfile
from builtins import BaseExceptionGroup
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from provider_core.api_config import https_trust


class _OptionalCertificateRejectionReset(logging.Filter):
    """Only one server-accept reset attributable to a rejected TLS handshake."""

    def __init__(self):
        super().__init__()
        self.records = []

    def filter(self, record):
        # Explicitly scoped by a test that separately proves client-side
        # certificate rejection. Unrelated or repeated errors still reach the
        # installed runner's strict asyncio ERROR observer.
        if (
            not self.records
            and record.name == "asyncio"
            and record.levelno == logging.ERROR
            and Path(record.pathname).parent.name == "asyncio"
            and Path(record.pathname).name == "base_events.py"
            and record.getMessage().split("\n", 1)[0]
            == "Error on transport creation for incoming connection"
            and record.exc_info is not None
            and type(record.exc_info[1]) is ConnectionResetError
        ):
            self.records.append(record)
            return False
        return True


@contextmanager
def expect_optional_certificate_rejection_reset():
    """Claim an optional server reset while a test rejects a TLS certificate.

    The negative test must independently assert the client-side rejection
    and absence of HTTP requests. asyncio can complete that rejection without
    logging a server-side error, so zero events is legitimate. This filter
    never changes the suite-wide policy for unrelated asyncio errors.
    """
    logger = logging.getLogger("asyncio")
    expected = _OptionalCertificateRejectionReset()
    logger.addFilter(expected)
    try:
        yield expected.records
    finally:
        logger.removeFilter(expected)


class TlsPeer:
    # silent-defaults: allow-contract -- The ordinary TLS peer has synthetic body/status and loopback trust; scenarios override those facts.
    def __init__(
        self,
        mode,
        *,
        response_body=b"data",
        response_bodies=None,
        response_headers=None,
        response_factory=None,
        response_status=200,
        response_mode=None,
        bind_host="127.0.0.1",
        handshake_seconds=3.0,
        shutdown_seconds=0.5,
    ):
        # One test-server TLS policy, distinct from HttpTransport's outbound
        # connect/exchange budget and the supervised interpreter child lifetime.
        for label, seconds in (
            ("handshake_seconds", handshake_seconds),
            ("shutdown_seconds", shutdown_seconds),
        ):
            if type(seconds) not in (int, float) or not 0 < seconds < float("inf"):
                raise ValueError(f"TlsPeer {label} must be finite positive seconds")
        self.handshake_seconds, self.shutdown_seconds = handshake_seconds, shutdown_seconds
        self.mode = mode
        self.response_body = response_body
        self.response_bodies = response_bodies
        self.response_headers = response_headers
        self.response_factory = response_factory
        self.response_status = response_status
        self.response_mode = response_mode
        self.bind_host = bind_host
        self.received = asyncio.Event()
        self.handshake_received = asyncio.Event()
        self.closed = asyncio.Event()
        self.handlers = set()
        self.requests = []
        self.request_bodies = []
        self.server_names = []
        self._closing = False

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider-core test peer")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(key, None)
        )
        (root / "key.pem").write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
        (root / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        server_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_trust.load_cert_chain(root / "cert.pem", root / "key.pem")

        def observe_server_name(_connection, server_name, _context):
            self.server_names.append(server_name)
            self.handshake_received.set()

        server_trust.set_servername_callback(observe_server_name)
        self.trust = https_trust((root / "cert.pem").read_bytes(), label="Test peer")
        tls = {}
        if self.mode != "tls-stall":
            # SSL handshakes precede handle(), hence they have no test-handler
            # task yet. Keep the server's two TLS limits independent, and give
            # teardown enough time to observe a pending handshake's own bound.
            tls = {
                "ssl": server_trust,
                "ssl_handshake_timeout": self.handshake_seconds,
                "ssl_shutdown_timeout": self.shutdown_seconds,
            }
        self.server = await asyncio.start_server(self.handle, self.bind_host, 0, **tls)
        self.origin = f"https://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            if self._closing:
                return
            if self.mode == "tls-stall":
                await reader.readexactly(1)
                self.received.set()
                await reader.read()
                self.closed.set()
                return
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            lengths = [
                int(line.split(b":", 1)[1]) for line in request.split(b"\r\n") if line.lower().startswith(b"content-length:")
            ]
            # silent-defaults: allow-contract -- Absent Content-Length means this fixture request has no framed body.
            length = lengths[0] if lengths else 0
            if len(lengths) > 1 or not 0 <= length <= 2097152:
                raise ValueError("Test request body exceeds the supported route bound or repeats Content-Length")
            self.request_bodies.append(await reader.readexactly(length))
            self.received.set()
            mode = self.mode
            if self.response_mode is not None:
                mode = self.response_mode(request, self.request_bodies[-1])
            if mode == "success":
                if self.response_factory is not None:
                    body = await self.response_factory(request, self.request_bodies[-1])
                else:
                    body = self.response_body if self.response_bodies is None else self.response_bodies.pop(0)
                if body is None:
                    # Simulate losing an acknowledgement after the full request
                    # arrived. Domain commit semantics belong to API qualification.
                    writer.close()
                    await writer.wait_closed()
                    self.closed.set()
                    return
                headers = self.response_headers
                if headers is None:
                    headers = ((b"Content-Type", b"application/json"), (b"Cache-Control", b"no-store"))
                writer.write(
                    f"HTTP/1.1 {self.response_status} {HTTPStatus(self.response_status).phrase}\r\n".encode("ascii")
                    + b"".join(name + b": " + value + b"\r\n" for name, value in headers)
                    + b"Content-Length: "
                    + str(len(body)).encode("ascii")
                    + b"\r\n\r\n"
                    + body
                )
            elif mode == "redirect":
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.invalid\r\nContent-Length: 0\r\n\r\n")
            elif mode in {"body-stall", "trickle", "disconnect"}:
                # silent-defaults: allow-contract -- No supplied response-header override means this fixture adds no extra headers.
                headers = () if self.response_headers is None else self.response_headers
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + b"".join(name + b": " + value + b"\r\n" for name, value in headers)
                    + b"Content-Length: 100\r\n\r\nx"
                )
            elif mode != "headers-stall":
                raise ValueError("Unknown test peer mode")
            await writer.drain()
            if mode == "disconnect":
                return
            if mode == "trickle":
                for _ in range(30):
                    await asyncio.sleep(0.02)
                    writer.write(b"x")
                    await writer.drain()
            await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            # A deliberately interrupted exchange may close either TLS direction.
            self.closed.set()
        else:
            self.closed.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                self.closed.set()

    async def __aexit__(self, *exception):
        self._closing = True
        self.server.close()
        handlers = tuple(self.handlers)
        cancelled = {task for task in handlers if not task.done()}
        for task in cancelled:
            task.cancel()
        try:
            # Pending TLS handshakes are not in self.handlers. Observe the
            # server through its configured handshake and shutdown allowances,
            # instead of an unrelated shorter two-second teardown ceiling.
            close_seconds = (
                2 if self.mode == "tls-stall"
                else max(2, self.handshake_seconds + self.shutdown_seconds + 0.5)
            )
            await asyncio.wait_for(self.server.wait_closed(), close_seconds)
            # A handshake can finish after the cancellation snapshot. Its late
            # closing handler also belongs to this fixture, including failures.
            handlers = tuple(self.handlers)
            results = await asyncio.wait_for(asyncio.gather(*handlers, return_exceptions=True), 2)
        finally:
            self.scratch.cleanup()
        failures = [
            result
            for task, result in zip(handlers, results, strict=True)
            if isinstance(result, BaseException) and not (task in cancelled and isinstance(result, asyncio.CancelledError))
        ]
        if failures:
            raise BaseExceptionGroup("TLS test peer failed", failures)
