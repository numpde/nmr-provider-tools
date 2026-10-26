"""Interface binding is mandatory for every proxy-owned outbound socket."""

from tools.container_guard import require_container

require_container()

import io
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools import bound_http_proxy


class BoundProxyTests(unittest.TestCase):
    def test_literal_ipv4_shortcut_never_starts_dns_or_socket_transport(self):
        with (
            patch.object(bound_http_proxy, "_dns_servers") as servers,
            patch.object(bound_http_proxy, "_query_ipv4") as query,
            patch.object(bound_http_proxy.socket, "socket") as transport,
        ):
            self.assertEqual(("192.0.2.1",), bound_http_proxy.resolve_ipv4_bound("192.0.2.1", "lo", deadline=30))
            with (
                patch.object(bound_http_proxy.ipaddress, "IPv4Address", side_effect=RuntimeError("classifier failure")),
                self.assertRaisesRegex(RuntimeError, "classifier failure"),
            ):
                bound_http_proxy.resolve_ipv4_bound("192.0.2.1", "lo", deadline=30)
        servers.assert_not_called()
        query.assert_not_called()
        transport.assert_not_called()

    def test_failed_admission_reply_preserves_original_safe_cause_and_unexpected_errors(self):
        original = ValueError("private-original-admission-marker")
        for reply_failure in (OSError("private-reply-marker"), RuntimeError("unexpected-reply-marker")):
            with self.subTest(reply_failure=type(reply_failure).__name__):
                request = MagicMock()
                request.sendall.side_effect = reply_failure
                errors = io.StringIO()
                with (
                    patch.object(bound_http_proxy, "receive_headers", side_effect=original),
                    patch.object(bound_http_proxy, "connect_bound") as connect,
                    redirect_stderr(errors),
                ):
                    if isinstance(reply_failure, OSError):
                        bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
                        self.assertEqual("proxy request admission failed: ValueError\n", errors.getvalue())
                    else:
                        with self.assertRaises(RuntimeError) as rejected:
                            bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
                        self.assertIs(reply_failure, rejected.exception)
                        self.assertEqual("", errors.getvalue())
                connect.assert_not_called()
                request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")

    def test_dns_name_depth_starts_at_zero_and_bounds_compression(self):
        encoded_name = b"\x03WWW\x07Example\x03COM\0"
        message = encoded_name + b"\x03API\xc0\x04"
        for offset, expected in (
            (0, ("www.example.com", len(encoded_name))),
            (len(encoded_name), ("api.example.com", len(message))),
        ):
            with self.subTest(offset=offset):
                self.assertEqual(expected, bound_http_proxy._read_dns_name(message, offset))
                self.assertEqual(expected, bound_http_proxy._read_dns_name(message, offset, 0))
        self.assertEqual(("www.example.com", len(encoded_name)), bound_http_proxy._read_dns_name(encoded_name, 0, 16))
        for encoded, offset, depth in ((message, len(encoded_name), 16), (b"", 0, 17)):
            with (
                self.subTest(offset=offset, depth=depth),
                self.assertRaisesRegex(ValueError, "compression pointer recursion is too deep"),
            ):
                bound_http_proxy._read_dns_name(encoded, offset, depth)
        with self.assertRaisesRegex(ValueError, "compression pointer recursion is too deep"):
            bound_http_proxy._read_dns_name(b"\xc0\x00", 0)

    def test_header_limit_accepts_exact_boundary_and_rejects_one_extra_byte(self):
        limit = bound_http_proxy.MAX_HEADER_BYTES
        header = b"x" * (limit - 4) + b"\r\n\r\n"
        client = MagicMock()
        client.recv.side_effect = [header[index : index + 8192] for index in range(0, len(header), 8192)]
        self.assertEqual(bound_http_proxy.receive_headers(client), (header, b""))
        for oversized in (b"x" * (limit - 3) + b"\r\n\r\n", b"x" * (limit + 1)):
            with self.subTest(terminated=oversized.endswith(b"\r\n\r\n")):
                client.recv.side_effect = [oversized[index : index + 8192] for index in range(0, len(oversized), 8192)]
                with self.assertRaisesRegex(ValueError, "headers"):
                    bound_http_proxy.receive_headers(client)

    def test_exact_limit_header_keeps_body_coalesced_with_its_final_fragment(self):
        header = b"x" * (bound_http_proxy.MAX_HEADER_BYTES - 4) + b"\r\n\r\n"
        body = b"body received with the terminator"
        prefix = header[:-1]
        client = MagicMock()
        client.recv.side_effect = [*[prefix[index : index + 8192] for index in range(0, len(prefix), 8192)], header[-1:] + body]
        self.assertEqual(bound_http_proxy.receive_headers(client), (header, body))

    def test_tiny_header_fragments_preserve_body_and_early_eof_rejects(self):
        header = b"GET http://example.invalid/ HTTP/1.1\r\n\r\n"
        body = b"body"
        client = MagicMock()
        client.recv.side_effect = [*[bytes([byte]) for byte in header[:-1]], header[-1:] + body]
        self.assertEqual(bound_http_proxy.receive_headers(client), (header, body))
        client.recv.side_effect = [header[:10], b""]
        with self.assertRaisesRegex(ConnectionError, "closed"):
            bound_http_proxy.receive_headers(client)

    def test_destination_ports_reject_out_of_range_or_noninteger_values_before_transport(self):
        private = "private-port-input-marker"
        for port in ("0", "65536", "-1", private):
            for target in ("CONNECT example.invalid:" + port, "GET http://example.invalid:" + port + "/"):
                with self.subTest(target=target), patch.object(bound_http_proxy, "connect_bound") as connect:
                    header = (target + " HTTP/1.1\r\n\r\n").encode("ascii")
                    with self.assertRaisesRegex(ValueError, "port.*1 to 65535") as rejected:
                        bound_http_proxy.parse_destination(header)
                    self.assertNotIn(private, str(rejected.exception))
                    request = MagicMock()
                    request.recv.return_value = header
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
                    connect.assert_not_called()
                    request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    self.assertIn("request admission", errors.getvalue())
                    self.assertIn("ValueError", errors.getvalue())
                    self.assertNotIn(private, errors.getvalue())

    def test_private_admission_failures_have_fixed_http_status_and_safe_diagnostics(self):
        private = "private-query-or-header-marker"
        for failure in (ValueError(private), OSError(private), UnicodeError(private)):
            with self.subTest(kind=type(failure).__name__):
                request = MagicMock()
                errors = io.StringIO()
                with (
                    patch.object(bound_http_proxy, "receive_headers", side_effect=failure),
                    patch.object(bound_http_proxy, "connect_bound") as connect,
                    redirect_stderr(errors),
                ):
                    bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
                connect.assert_not_called()
                request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                self.assertIn("request admission", errors.getvalue())
                self.assertIn(type(failure).__name__, errors.getvalue())
                self.assertNotIn(private, errors.getvalue())

    def test_forwarding_attempt_failure_never_appends_an_http_status(self):
        private = "private-forwarding-failure-marker"
        for phase in ("http_header", "connect_status", "connect_remainder", "relay"):
            with self.subTest(phase=phase):
                request, outbound = MagicMock(), MagicMock()
                outbound.__enter__.return_value = outbound
                # silent-defaults: allow-contract -- The failure-phase table selects HTTP header forwarding or the CONNECT handshake; each case checks which write can occur before failure; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_attempt_failure_never_appends_an_http_status
                header = (
                    b"GET http://example.invalid/ HTTP/1.1\r\n\r\n"
                    if phase == "http_header"
                    else b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n"
                )
                # silent-defaults: allow-contract -- Only the CONNECT remainder case supplies coalesced body bytes; the other failure phases deliberately have no remainder; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_attempt_failure_never_appends_an_http_status
                body = b"body" if phase == "connect_remainder" else b""
                if phase == "connect_status":
                    request.sendall.side_effect = OSError(private)
                elif phase in {"http_header", "connect_remainder"}:
                    outbound.sendall.side_effect = OSError(private)
                errors = io.StringIO()
                with (
                    patch.object(bound_http_proxy, "receive_headers", return_value=(header, body)),
                    patch.object(bound_http_proxy, "connect_bound", return_value=outbound),
                    patch.object(bound_http_proxy, "relay", side_effect=OSError(private)) as relay,
                    redirect_stderr(errors),
                ):
                    bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
                if phase == "http_header":
                    request.sendall.assert_not_called()
                else:
                    request.sendall.assert_called_once_with(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if phase == "relay":
                    relay.assert_called_once_with(request, outbound)
                else:
                    relay.assert_not_called()
                self.assertIn("forwarding", errors.getvalue())
                self.assertIn("OSError", errors.getvalue())
                self.assertNotIn(private, errors.getvalue())

    def test_destination_port_boundaries_and_scheme_defaults_are_preserved(self):
        for port in (1, 65535):
            for method, target in (
                ("CONNECT", "example.invalid:" + str(port)),
                ("GET", "http://example.invalid:" + str(port) + "/path?query=1"),
            ):
                with self.subTest(method=method, port=port):
                    header = f"{method} {target} HTTP/1.1\r\n\r\n".encode("ascii")
                    result = bound_http_proxy.parse_destination(header)
                    self.assertEqual(result[:3], (method, "example.invalid", port))
                    # silent-defaults: allow-contract -- CONNECT preserves request bytes while HTTP forwards the origin-form target; the expected wire bytes are checked separately from parsed host and port; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_destination_port_boundaries_and_scheme_defaults_are_preserved
                    self.assertEqual(result[3], header if method == "CONNECT" else b"GET /path?query=1 HTTP/1.1\r\n\r\n")
        for scheme, port in (("http", 80), ("https", 443)):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    bound_http_proxy.parse_destination(f"GET {scheme}://example.invalid/ HTTP/1.1\r\n\r\n".encode("ascii"))[2],
                    port,
                )

    def test_empty_absolute_path_becomes_origin_slash(self):
        # RFC 9112 section 3.2.1 owns the path, including a query-only URI:
        # https://www.rfc-editor.org/rfc/rfc9112.html#section-3.2.1
        for suffix, origin in (("", "/"), ("?q=one%20two", "/?q=one%20two")):
            with self.subTest(suffix=suffix):
                header = f"GET http://example.invalid{suffix} HTTP/1.1\r\nHost: example.invalid\r\n\r\n".encode("ascii")
                method, host, port, forwarded = bound_http_proxy.parse_destination(header)
                self.assertEqual(("GET", "example.invalid", 80), (method, host, port))
                self.assertEqual(
                    f"GET {origin} HTTP/1.1\r\nHost: example.invalid\r\n\r\n".encode("ascii"),
                    forwarded,
                )

    def test_forwarding_owns_write_budget_after_header_deadline_nearly_expires(self):
        """A valid late header retains the full bounded first-write budget."""
        for method in ("GET", "CONNECT"):
            with self.subTest(method=method):
                self._assert_forwarding_write_budget(method)

    def _assert_forwarding_write_budget(self, method):
        """Observe each peer's timeout at the actual first forwarding write."""
        clock, observed = [0], []
        request, outbound = MagicMock(), MagicMock()
        outbound.__enter__.return_value = outbound
        # silent-defaults: allow-contract -- The GET and CONNECT cases choose their respective request-target forms before observing both peers' first-write timeout settings; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_owns_write_budget_after_header_deadline_nearly_expires
        target = "http://example.invalid/" if method == "GET" else "example.invalid:443"
        header = f"{method} {target} HTTP/1.1\r\n\r\n".encode("ascii")
        fragments = iter(((29, header[:-1]), (29.9, header[-1:])))

        def receive(_size):
            clock[0], fragment = next(fragments)
            return fragment

        def sent(_data):
            observed.append((request.settimeout.call_args.args[0], outbound.settimeout.call_args.args[0]))

        request.recv.side_effect = receive
        writer = outbound if method == "GET" else request
        writer.sendall.side_effect = sent
        with (
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bound_http_proxy, "connect_bound", return_value=outbound),
            patch.object(bound_http_proxy, "relay"),
        ):
            bound_http_proxy.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo"))
        self.assertEqual([call.args[0] for call in request.settimeout.call_args_list], [30, 1, 120])
        self.assertEqual(observed, [(120, 120)])

    def test_connect_binds_before_connecting(self):
        outbound = MagicMock()
        with (
            patch.object(bound_http_proxy, "resolve_ipv4_bound", return_value=("192.0.2.1",)),
            patch.object(socket, "socket", return_value=outbound),
        ):
            self.assertIs(bound_http_proxy.connect_bound("example.invalid", 443, "lo"), outbound)
        self.assertEqual(outbound.method_calls[0].args, (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0"))
        outbound.connect.assert_called_once_with(("192.0.2.1", 443))

    def test_bind_failure_never_connects_or_falls_back(self):
        outbound = MagicMock()
        outbound.setsockopt.side_effect = PermissionError("binding denied")
        with (
            patch.object(bound_http_proxy, "resolve_ipv4_bound", return_value=("192.0.2.1",)),
            patch.object(socket, "socket", return_value=outbound),
            self.assertRaises(PermissionError),
        ):
            bound_http_proxy.connect_bound("example.invalid", 443, "lo")
        outbound.connect.assert_not_called()
        outbound.close.assert_called_once_with()

    def test_startup_binding_failure_cannot_publish_readiness(self):
        """The local no-transfer binding probe must succeed before readiness."""
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            probe = MagicMock()
            probe.__enter__.return_value = probe
            probe.setsockopt.side_effect = PermissionError("scripted binding failure")
            with (
                patch.object(bound_http_proxy, "require_interface"),
                patch.object(bound_http_proxy.socket, "socket", return_value=probe),
                patch.object(bound_http_proxy, "ProxyServer") as server,
                patch.object(
                    bound_http_proxy.sys, "argv", ["proxy", "--interface", "eth0", "--port", "0", "--ready-file", str(ready)]
                ),
                self.assertRaises(PermissionError),
            ):
                bound_http_proxy.main()
            self.assertFalse(ready.exists())
            server.assert_not_called()
            probe.connect.assert_not_called()

    def test_omitted_listener_port_rejects_before_interface_socket_and_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            stderr = io.StringIO()
            with (
                patch.object(bound_http_proxy, "require_interface") as interface,
                patch.object(bound_http_proxy.socket, "socket") as probe,
                patch.object(bound_http_proxy, "ProxyServer") as server,
                patch.object(bound_http_proxy.sys, "argv", ["proxy", "--interface", "eth0", "--ready-file", str(ready)]),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as rejected,
            ):
                bound_http_proxy.main()
            self.assertEqual(2, rejected.exception.code)
            self.assertIn("--port", stderr.getvalue())
            interface.assert_not_called()
            probe.assert_not_called()
            server.assert_not_called()
            self.assertFalse(ready.exists())

    def test_explicit_listener_ports_reach_the_bind_and_publish_its_actual_port(self):
        for requested, observed in ((0, 23456), (12345, 12345)):
            with self.subTest(port=requested), tempfile.TemporaryDirectory() as temporary:
                ready = Path(temporary) / "proxy.port"
                listener = MagicMock()
                listener.server_address = ("127.0.0.1", observed)
                with (
                    patch.object(bound_http_proxy, "require_interface"),
                    patch.object(bound_http_proxy.socket, "socket"),
                    patch.object(bound_http_proxy, "ProxyServer") as server,
                    patch.object(
                        bound_http_proxy.sys,
                        "argv",
                        ["proxy", "--interface", "eth0", "--port", str(requested), "--ready-file", str(ready)],
                    ),
                ):
                    server.return_value.__enter__.return_value = listener
                    bound_http_proxy.main()
                server.assert_called_once_with(("127.0.0.1", requested), "eth0")
                self.assertEqual(f"{observed}\n".encode(), ready.read_bytes())
                listener.serve_forever.assert_called_once_with()
