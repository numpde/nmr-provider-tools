"""Unix gateway and loopback relay preserve HTTP/CONNECT without IP fallback."""

from provider_core.container_guard import require_container

require_container()

import io
import socket
import socketserver
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from provider_core import bound_http_proxy


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(2)
            if thread.is_alive():
                raise RuntimeError("Proxy test listener did not stop.")


def read_all(client):
    data = bytearray()
    while block := client.recv(4096):
        data.extend(block)
    return bytes(data)


class HTTPResponse(socketserver.BaseRequestHandler):
    def handle(self):
        header, _ = bound_http_proxy.receive_headers(self.request)
        self.server.headers.append(header)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


class EchoResponse(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        with self.request.makefile("rb") as stream:
            self.request.sendall(stream.read(4))


class UnixProxyTests(unittest.TestCase):
    def test_http_request_crosses_relay_and_unix_gateway(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gateway.sock"
            with serving(socketserver.TCPServer(("127.0.0.1", 0), HTTPResponse)) as origin:
                origin.headers = []
                with serving(bound_http_proxy.UnixProxyServer(path, "system")):
                    self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                    with serving(bound_http_proxy.UnixRelayServer(("127.0.0.1", 0), path)) as relay:
                        with socket.create_connection(relay.server_address, timeout=2) as client:
                            client.sendall(f"GET http://127.0.0.1:{origin.server_address[1]}/proof HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
                            client.shutdown(socket.SHUT_WR)
                            response = read_all(client)
                self.assertTrue(response.endswith(b"\r\n\r\nok"))
                self.assertEqual([b"GET /proof HTTP/1.1\r\nHost: fixture\r\n\r\n"], origin.headers)

    def test_connect_tunnel_crosses_relay_without_a_second_success_reply(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gateway.sock"
            with serving(socketserver.TCPServer(("127.0.0.1", 0), EchoResponse)) as origin:
                with serving(bound_http_proxy.UnixProxyServer(path, "system")):
                    with serving(bound_http_proxy.UnixRelayServer(("127.0.0.1", 0), path)) as relay:
                        with socket.create_connection(relay.server_address, timeout=2) as client:
                            client.sendall(f"CONNECT 127.0.0.1:{origin.server_address[1]} HTTP/1.1\r\nHost: fixture\r\n\r\nping".encode())
                            client.shutdown(socket.SHUT_WR)
                            response = read_all(client)
            self.assertEqual(b"HTTP/1.1 200 Connection Established\r\n\r\nping", response)

    def test_missing_unix_gateway_fails_closed_without_destination_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "absent.sock"
            errors = io.StringIO()
            with patch.object(bound_http_proxy, "connect_bound") as connect, redirect_stderr(errors):
                with serving(bound_http_proxy.UnixRelayServer(("127.0.0.1", 0), path)) as relay:
                    with socket.create_connection(relay.server_address, timeout=2) as client:
                        response = read_all(client)
                connect.assert_not_called()
            self.assertEqual(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n", response)
            self.assertIn("FileNotFoundError", errors.getvalue())
            self.assertNotIn(str(path), errors.getvalue())

    def test_existing_unix_listener_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gateway.sock"
            path.write_bytes(b"owned by another operation")
            with self.assertRaises(OSError):
                bound_http_proxy.UnixProxyServer(path, "system")
            self.assertEqual(b"owned by another operation", path.read_bytes())

    def test_unix_gateway_and_relay_publish_their_distinct_readiness(self):
        cases = (
            (["--interface", "system", "--unix-socket", "/gateway/proxy.sock"],
             "UnixProxyServer", "/gateway/proxy.sock", "/gateway/proxy.sock\n"),
            (["--relay-socket", "/gateway/proxy.sock", "--port", "0"],
             "UnixRelayServer", ("127.0.0.1", 23456), "23456\n"),
        )
        for arguments, owner, address, expected in cases:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                ready = Path(temporary) / "ready"
                server = MagicMock()
                server.server_address = address
                with (
                    patch.object(bound_http_proxy, owner) as listener,
                    patch.object(bound_http_proxy, "require_interface") as interface,
                    patch.object(bound_http_proxy.sys, "argv", ["proxy", *arguments, "--ready-file", str(ready)]),
                ):
                    listener.return_value.__enter__.return_value = server
                    bound_http_proxy.main()
                self.assertEqual(expected, ready.read_text())
                interface.assert_not_called()
                server.serve_forever.assert_called_once_with()
