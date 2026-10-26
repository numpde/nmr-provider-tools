"""Finite TLS peer; explicit bind address, disposable keys and certificates."""

import asyncio
import ipaddress
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
    ):
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
        self.trust = ssl.create_default_context(cafile=str(root / "cert.pem"))
        tls = {}
        if self.mode != "tls-stall":
            # Accepted TLS handshakes precede handle(), so its task set cannot
            # retire them. Native TLS deadlines must fit inside teardown's 2s.
            tls = {"ssl": server_trust, "ssl_handshake_timeout": 0.5, "ssl_shutdown_timeout": 0.5}
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
            await asyncio.wait_for(self.server.wait_closed(), 2)
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
