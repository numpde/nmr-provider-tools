"""Interface binding is mandatory for every proxy-owned outbound socket."""

from provider_core.container_guard import require_container

require_container()

import io
import socket
import struct
import tempfile
import unittest
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from provider_core import bound_http_proxy
from provider_core.adapters import http_proxy
from provider_core.adapters.dns import encode_dns_name
from tests.fakes.proxy_logs import capture_proxy_logs


def _dns_response(host):
    """Answer one fixed IPv4 question using a legal compressed owner name."""

    return (
        struct.pack("!HHHHHH", 111, 0x8180, 1, 1, 0, 0) +
        encode_dns_name(host) +
        struct.pack("!HH", 1, 1) +
        b"\xc0\x0c" +
        struct.pack("!HHIH", 1, 1, 60, 4) +
        b"\xcb\x00\x71\x01"
    )


class DNSQueryTests(unittest.TestCase):
    def test_dns_binds_device_and_shares_one_query_budget(self):
        clock = [0]
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver
        resolver.connect.side_effect = lambda _address: clock.__setitem__(0, 0.2)
        resolver.send.side_effect = lambda _dns_query: clock.__setitem__(0, 0.6)
        resolver.recv.return_value = _dns_response("example.invalid")

        with (
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bound_http_proxy.secrets, "randbits", return_value=111),
            patch.object(bound_http_proxy.socket, "socket", return_value=resolver),
        ):
            (addresses, aliases) = bound_http_proxy._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)

        self.assertEqual(addresses, {"example.invalid": ["203.0.113.1"]})
        self.assertEqual(aliases, {})
        resolver.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth0\0")
        resolver.connect.assert_called_once_with(("203.0.113.53", 53))
        self.assertEqual([call.args[0] for call in resolver.settimeout.call_args_list], [3, 2.8, 2.4])
        self.assertEqual(resolver.send.call_args.args[0][12:], encode_dns_name("example.invalid") + struct.pack("!HH", 1, 1))

    def test_dns_rejects_a_reply_for_another_question(self):
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver
        resolver.recv.return_value = _dns_response("other.invalid")

        with (
            patch.object(bound_http_proxy.time, "monotonic", return_value=0),
            patch.object(bound_http_proxy.secrets, "randbits", return_value=111),
            patch.object(bound_http_proxy.socket, "socket", return_value=resolver),
            self.assertRaises(OSError),
        ):
            bound_http_proxy._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)

    def test_dns_late_success_rejects_the_reply_and_closes_the_socket(self):
        """A complete reply arriving after the query budget is not admitted.

        This catches the inherited missing post-recv check at:
        https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L183-L184
        """
        clock = [0]
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver

        def late_reply(_size):
            clock[0] = 3.1

            return _dns_response("example.invalid")

        resolver.recv.side_effect = late_reply

        with (
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bound_http_proxy.secrets, "randbits", return_value=111),
            patch.object(bound_http_proxy.socket, "socket", return_value=resolver),
            self.assertRaises(TimeoutError),
        ):
            bound_http_proxy._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)

        resolver.__exit__.assert_called_once()

    def test_dns_query_deadline_caps_wait_at_three_seconds(self):
        clock = [0]
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver

        resolver.connect.side_effect = lambda _address: clock.__setitem__(0, 1)
        resolver.send.side_effect = lambda _dns_query: clock.__setitem__(0, 3)

        with (
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bound_http_proxy.secrets, "randbits", return_value=111),
            patch.object(bound_http_proxy.socket, "socket", return_value=resolver),
            self.assertRaises(TimeoutError),
        ):
            bound_http_proxy._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)

        self.assertEqual([call.args[0] for call in resolver.settimeout.call_args_list], [3, 2])
        resolver.recv.assert_not_called()
        self.assertEqual(3, clock[0])


class BoundProxyTests(unittest.TestCase):
    def test_explicit_resolver_file_cannot_fall_back_to_global_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            resolver_path = pathlib.Path(directory) / "resolv.conf"

            for content in ("", "nameserver 127.0.0.53\n", "nameserver invalid\n"):
                resolver_path.write_text(content)

                with self.subTest(content=content), self.assertRaises(bound_http_proxy.ProxyResolutionFailed):
                    bound_http_proxy._dns_servers(resolver_path)

            resolver_path.write_text("nameserver 192.0.2.53\n")
            self.assertEqual(bound_http_proxy._dns_servers(resolver_path), ("192.0.2.53",))

    def test_captured_resolvers_remain_bound_without_rereading_configuration(self):
        with patch.object(bound_http_proxy, "_dns_servers") as discover, \
                patch.object(bound_http_proxy, "_query_ipv4", return_value=({"api.example": ["192.0.2.1"]}, {})) as query, \
                patch.object(bound_http_proxy.time, "monotonic", return_value=0):
            addresses = bound_http_proxy.resolve_ipv4_bound(
                "api.example",
                "wlan0",
                deadline=30,
                dns_servers=("192.0.2.53",)
            )

        self.assertEqual(addresses, ("192.0.2.1",))
        query.assert_called_once_with("api.example", "192.0.2.53", "wlan0", deadline=30)
        discover.assert_not_called()

    def test_capacity_can_change_without_displacing_active_connections(self):
        slots = bound_http_proxy.ConnectionAdmission(2)

        self.assertTrue(slots.acquire())
        self.assertTrue(slots.acquire())
        self.assertFalse(slots.acquire())
        slots.set_limit(1)
        slots.release()
        self.assertFalse(slots.acquire())
        slots.set_limit(2)
        self.assertTrue(slots.acquire())
        slots.release()
        slots.release()
        self.assertTrue(slots.acquire())
        slots.release()

    def test_failed_worker_start_releases_capacity_for_the_next_request(self):
        with bound_http_proxy.ProxyServer(("127.0.0.1", 0), "lo") as server:
            server._slots.set_limit(1)

            with patch.object(bound_http_proxy.socketserver.ThreadingMixIn, "process_request", side_effect=RuntimeError("cannot start worker")):
                with self.assertRaisesRegex(RuntimeError, "cannot start worker"):
                    server.process_request(MagicMock(), ("127.0.0.1", 1))

            with patch.object(bound_http_proxy.socketserver.ThreadingMixIn, "process_request") as start:
                server.process_request(MagicMock(), ("127.0.0.1", 2))
                start.assert_called_once()

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
        original = ValueError("original admission failure")

        for reply_failure in (OSError("failure reply could not be written"), RuntimeError("unexpected-reply-marker")):
            with self.subTest(reply_failure=type(reply_failure).__name__):
                client_socket = MagicMock()
                client_socket.sendall.side_effect = reply_failure
                errors = io.StringIO()

                with (
                    patch.object(bound_http_proxy, "receive_headers", side_effect=original),
                    patch.object(bound_http_proxy, "connect_bound") as connect,
                    capture_proxy_logs(errors),
                ):
                    if isinstance(reply_failure, OSError):
                        bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))
                        self.assertIn(str(original), errors.getvalue())
                        self.assertIn("failure reply could not be written", errors.getvalue())
                    else:
                        with self.assertRaises(RuntimeError) as rejected:
                            bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))

                        self.assertIs(reply_failure, rejected.exception)
                        self.assertEqual("", errors.getvalue())

                connect.assert_not_called()

                client_socket.sendall.assert_called_once_with(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )

    def test_header_limit_accepts_exact_boundary_and_rejects_one_extra_byte(self):
        max_header_bytes = bound_http_proxy.MAX_HEADER_BYTES

        header = b"x" * (max_header_bytes - 4) + (
            b"\r\n"
            b"\r\n"
        )

        client = MagicMock()
        client.recv.side_effect = [header[index : index + 8192] for index in range(0, len(header), 8192)]

        self.assertEqual(bound_http_proxy.receive_headers(client), (header, b""))

        for oversized in (
            b"x" * (max_header_bytes - 3) + (
                b"\r\n"
                b"\r\n"
            ),
            b"x" * (max_header_bytes + 1)
        ):
            with self.subTest(
                terminated=oversized.endswith(
                    b"\r\n"
                    b"\r\n"
                )
            ):
                client.recv.side_effect = [oversized[index : index + 8192] for index in range(0, len(oversized), 8192)]

                with self.assertRaisesRegex(ValueError, "header_byte_count=.* should be at most MAX_HEADER_BYTES="):
                    bound_http_proxy.receive_headers(client)

    def test_exact_limit_header_keeps_body_coalesced_with_its_final_fragment(self):
        header = b"x" * (bound_http_proxy.MAX_HEADER_BYTES - 4) + (
            b"\r\n"
            b"\r\n"
        )

        body = b"body received with the terminator"
        prefix = header[:-1]
        client = MagicMock()
        client.recv.side_effect = [*[prefix[index : index + 8192] for index in range(0, len(prefix), 8192)], header[-1:] + body]

        self.assertEqual(bound_http_proxy.receive_headers(client), (header, body))

    def test_tiny_header_fragments_preserve_body_and_early_eof_rejects(self):
        header = (
            b"GET http://example.invalid/ HTTP/1.1\r\n"
            b"\r\n"
        )

        body = b"body"
        client = MagicMock()
        client.recv.side_effect = [*[bytes([byte]) for byte in header[:-1]], header[-1:] + body]

        self.assertEqual(bound_http_proxy.receive_headers(client), (header, body))
        client.recv.side_effect = [header[:10], b""]

        with self.assertRaises(ConnectionError):
            bound_http_proxy.receive_headers(client)

    def test_destination_ports_reject_out_of_range_or_noninteger_values_before_transport(self):
        private = "private-port-input-marker"

        for port in ("0", "65536", "-1", private):
            for target in ("CONNECT example.invalid:" + port, "GET http://example.invalid:" + port + "/"):
                with self.subTest(target=target), patch.object(bound_http_proxy, "connect_bound") as connect:
                    header = (target + (
                        " HTTP/1.1\r\n"
                        "\r\n"
                    )).encode("ascii")

                    with self.assertRaises(ValueError) as rejected:
                        http_proxy.parse_destination(header)

                    client_socket = MagicMock()
                    client_socket.recv.return_value = header
                    errors = io.StringIO()

                    with capture_proxy_logs(errors):
                        bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))

                    connect.assert_not_called()

                    client_socket.sendall.assert_called_once_with(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Connection: close\r\n"
                        b"\r\n"
                    )

                    self.assertIn("request admission", errors.getvalue())
                    self.assertIn("ValueError", errors.getvalue())

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_destination_rejection_omits_port_input(self):
        private = "private-port-input-marker"

        with self.assertRaises(ValueError) as rejected:
            http_proxy.parse_destination(
                (
                    f"CONNECT example.invalid:{private} HTTP/1.1\r\n"
                    f"\r\n"
                ).encode(),
            )

        self.assertNotIn(private, str(rejected.exception))

    def _admission_failure_replies(self):
        private = "private-query-or-header-marker"

        for failure in (ValueError("Bearer " + private), OSError("Bearer " + private), UnicodeError("Bearer " + private)):
            with self.subTest(kind=type(failure).__name__):
                client_socket = MagicMock()
                errors = io.StringIO()

                with (
                    patch.object(bound_http_proxy, "receive_headers", side_effect=failure),
                    patch.object(bound_http_proxy, "connect_bound") as connect,
                    capture_proxy_logs(errors),
                ):
                    bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))

                yield (failure, client_socket, connect, errors.getvalue(), private)

    def test_admission_failures_keep_fixed_http_status(self):
        for (failure, client_socket, connect, rendered, _private) in self._admission_failure_replies():
            connect.assert_not_called()

            client_socket.sendall.assert_called_once_with(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )

            self.assertIn("request admission", rendered)
            self.assertIn(type(failure).__name__, rendered)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_admission_failure_logs_scrub_credentials(self):
        for (_failure, _client_socket, _connect, rendered, private) in self._admission_failure_replies():
            self.assertNotIn(private, rendered)

    def test_forwarding_attempt_failure_never_appends_an_http_status(self):
        private = "native-forwarding-failure-marker"

        for phase in ("http_header", "connect_status", "connect_remainder", "relay"):
            with self.subTest(phase=phase):
                (client_socket, outbound) = (MagicMock(), MagicMock())
                outbound.__enter__.return_value = outbound

                # silent-defaults: allow-contract -- The failure-phase table selects HTTP header forwarding or the CONNECT handshake; each case checks which write can occur before failure; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_attempt_failure_never_appends_an_http_status
                header = (
                    b"GET http://example.invalid/ HTTP/1.1\r\n"
                    b"\r\n"
                    if phase == "http_header"
                    else (
                        b"CONNECT example.invalid:443 HTTP/1.1\r\n"
                        b"\r\n"
                    )
                )

                # silent-defaults: allow-contract -- Only the CONNECT remainder case supplies coalesced body bytes; the other failure phases deliberately have no remainder; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_attempt_failure_never_appends_an_http_status
                body = b"body" if phase == "connect_remainder" else b""

                if phase == "connect_status":
                    client_socket.sendall.side_effect = OSError(private)
                elif phase in {"http_header", "connect_remainder"}:
                    outbound.sendall.side_effect = OSError(private)

                errors = io.StringIO()

                with (
                    patch.object(bound_http_proxy, "receive_headers", return_value=(header, body)),
                    patch.object(bound_http_proxy, "connect_bound", return_value=outbound),
                    patch.object(bound_http_proxy, "relay", side_effect=OSError(private)) as relay,
                    capture_proxy_logs(errors),
                ):
                    bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))

                if phase == "http_header":
                    client_socket.sendall.assert_not_called()
                else:
                    client_socket.sendall.assert_called_once_with(
                        b"HTTP/1.1 200 Connection Established\r\n"
                        b"\r\n"
                    )

                if phase == "relay":
                    relay.assert_called_once_with(client_socket, outbound)
                else:
                    relay.assert_not_called()

                self.assertIn("forwarding", errors.getvalue())
                self.assertIn("OSError", errors.getvalue())
                self.assertIn(private, errors.getvalue())

    def test_forwarding_owns_write_budget_after_header_deadline_nearly_expires(self):
        """A valid late header retains the full bounded first-write budget."""

        for method in ("GET", "CONNECT"):
            with self.subTest(method=method):
                self._assert_forwarding_write_budget(method)

    def _assert_forwarding_write_budget(self, method):
        """Observe each peer's timeout at the actual first forwarding write."""
        (clock, observed) = ([0], [])
        (client_socket, outbound) = (MagicMock(), MagicMock())
        outbound.__enter__.return_value = outbound

        # silent-defaults: allow-contract -- The GET and CONNECT cases choose their respective request-target forms before observing both peers' first-write timeout settings; proof=tests/repository/test_bound_http_proxy.py::BoundProxyTests.test_forwarding_owns_write_budget_after_header_deadline_nearly_expires
        target = "http://example.invalid/" if method == "GET" else "example.invalid:443"

        header = (
            f"{method} {target} HTTP/1.1\r\n"
            f"\r\n"
        ).encode("ascii")

        fragments = iter(((29, header[:-1]), (29.9, header[-1:])))

        def receive(_size):
            (clock[0], fragment) = next(fragments)

            return fragment

        def sent(_request_bytes):
            observed.append((client_socket.settimeout.call_args.args[0], outbound.settimeout.call_args.args[0]))

        client_socket.recv.side_effect = receive
        writer = outbound if method == "GET" else client_socket
        writer.sendall.side_effect = sent

        with (
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bound_http_proxy, "connect_bound", return_value=outbound),
            patch.object(bound_http_proxy, "relay"),
        ):
            bound_http_proxy.ProxyHandler(client_socket, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="lo", dns_servers=None))

        self.assertEqual([call.args[0] for call in client_socket.settimeout.call_args_list], [30, 1, 120])
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
            ready = pathlib.Path(temporary) / "proxy.port"
            probe = MagicMock()
            probe.__enter__.return_value = probe
            probe.setsockopt.side_effect = PermissionError("scripted binding failure")

            with (
                patch.object(bound_http_proxy, "require_interface"),
                patch.object(bound_http_proxy.socket, "socket", return_value=probe),
                patch.object(bound_http_proxy, "ProxyServer") as server,
                patch.object(
                    bound_http_proxy.sys,
                    "argv",
                    ["proxy", "--interface", "eth0", "--port", "0", "--ready-file", str(ready)]
                ),
                self.assertRaises(PermissionError),
            ):
                bound_http_proxy.main()

            self.assertFalse(ready.exists())
            server.assert_not_called()
            probe.connect.assert_not_called()

    def test_omitted_listener_port_rejects_before_interface_socket_and_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = pathlib.Path(temporary) / "proxy.port"
            stderr = io.StringIO()

            with (
                patch.object(bound_http_proxy, "require_interface") as interface,
                patch.object(bound_http_proxy.socket, "socket") as probe,
                patch.object(bound_http_proxy, "ProxyServer") as server,
                patch.object(bound_http_proxy.sys, "argv", ["proxy", "--interface", "eth0", "--ready-file", str(ready)]),
                capture_proxy_logs(stderr),
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
        for (requested, observed) in ((0, 23456), (12345, 12345)):
            with self.subTest(port=requested), tempfile.TemporaryDirectory() as temporary:
                ready = pathlib.Path(temporary) / "proxy.port"
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

                server.assert_called_once_with(("127.0.0.1", requested), "eth0", dns_servers=None)
                self.assertEqual(f"{observed}\n".encode(), ready.read_bytes())
                listener.serve_forever.assert_called_once_with()
