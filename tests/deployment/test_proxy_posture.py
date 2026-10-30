"""Exercise proxy protocol/resource boundaries with injected sockets and time.

No listener, DNS server, interface traffic or installer runs in this offline
lane. Device-binding calls, deadline accounting and disclosure are observable;
real routing and maintained build lifecycle require separate live proof.

All 21 original methods and adversarial cases are preserved from:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py
The production module path and explicit absent-primitive admission failures are
local adaptations. Additional late-success and publication cases expose
inherited deadline and effect-reporting gaps. No Magnet filenames or scientific imports are required.
"""

from provider_core.container_guard import require_container

require_container()

import io
import stat
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

# Exercise the installed owner; source checkout paths cannot satisfy this import.
from provider_core import bound_http_proxy as _PROXY

# A fixed adversarial marker proves that request-bearing errors stay private.
# repo-check: allow-module-constant
_PRIVATE = "private-request-query-or-header-marker"


class ImageBuildProxyInputTests(unittest.TestCase):
    def setUp(self):
        required = {"test_generic_interface_admission_uses_native_lookup": "require_interface"}.get(self._testMethodName)
        if self._testMethodName in {
            "test_complete_headers_preserve_the_received_remainder",
            "test_header_drip_and_late_complete_fragment_do_not_reset_deadline",
        }:
            required = "time"
        if required:
            self.assertTrue(
                hasattr(_PROXY, required),
                msg=f"Proxy admission cannot prove {self._testMethodName}: provider_core.bound_http_proxy does not own {required}.",
            )

    def test_generic_interface_admission_uses_native_lookup(self):
        with patch.object(_PROXY.socket, "if_nametoindex", return_value=1) as lookup:
            _PROXY.require_interface("eth0")
        lookup.assert_called_once_with("eth0")
        with patch.object(_PROXY.socket, "if_nametoindex") as lookup:
            for invalid in ("", ".", "..", "../eth0", "eth0:1", "eth 0", "éth0", "x" * 16):
                with self.subTest(interface=invalid), self.assertRaises(ValueError):
                    _PROXY.require_interface(invalid)
            lookup.assert_not_called()
        with (
            patch.object(_PROXY.socket, "if_nametoindex", side_effect=OSError(_PRIVATE)),
            self.assertRaises(ValueError) as caught,
        ):
            _PROXY.require_interface("eth0")
        self.assertNotIn(_PRIVATE, str(caught.exception))

    def test_connect_ports_are_admitted_before_socket_use(self):
        for port in ("0", "65536", "-1", "not-a-port"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                _PROXY.parse_destination(f"CONNECT example.invalid:{port} HTTP/1.1\r\n\r\n".encode())
        self.assertEqual(
            ("CONNECT", "example.invalid", 443), _PROXY.parse_destination(b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n")[:3]
        )

    def test_complete_headers_preserve_the_received_remainder(self):
        client = Mock()
        client.recv.return_value = b"GET http://example.invalid/ HTTP/1.1\r\n\r\nbody"
        with patch.object(_PROXY.time, "monotonic", return_value=0):
            self.assertEqual((b"GET http://example.invalid/ HTTP/1.1\r\n\r\n", b"body"), _PROXY.receive_headers(client))
        client.settimeout.assert_called_once_with(30)

    def test_header_drip_and_late_complete_fragment_do_not_reset_deadline(self):
        for final in (None, b"\r\n\r\n"):
            with self.subTest(final_complete=final is not None):
                self._assert_header_deadline(final)

    def _assert_header_deadline(self, final):
        clock = [0]
        client = Mock()
        fragments = iter(((14, b"x"), (28, b"x"), (31, final)))

        def receive(_size):
            clock[0], fragment = next(fragments)
            if fragment is None:
                raise TimeoutError("scripted socket deadline")
            return fragment

        client.recv.side_effect = receive
        with patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]), self.assertRaises(TimeoutError):
            _PROXY.receive_headers(client)
        self.assertEqual([call.args[0] for call in client.settimeout.call_args_list], [30, 16, 2])

    def test_header_limit_accepts_exact_boundary_and_rejects_one_byte_more(self):
        header_end = b"\r\n\r\n"
        exact = Mock()
        raw = b"x" * (_PROXY.MAX_HEADER_BYTES - len(header_end)) + header_end
        exact.recv.side_effect = [raw[index : index + 8192] for index in range(0, len(raw), 8192)]
        self.assertEqual(len(_PROXY.receive_headers(exact)[0]), _PROXY.MAX_HEADER_BYTES)
        self.assertEqual(8, exact.recv.call_count)

        oversized = Mock()
        raw = b"x" * (_PROXY.MAX_HEADER_BYTES + 1)
        oversized.recv.side_effect = [raw[index : index + 8192] for index in range(0, len(raw), 8192)]
        with self.assertRaises(ValueError):
            _PROXY.receive_headers(oversized)
        self.assertEqual(9, oversized.recv.call_count)

    def test_header_limit_excludes_a_coalesced_body_remainder(self):
        header = b"x" * (_PROXY.MAX_HEADER_BYTES - 4) + b"\r\n\r\n"
        prefix, body = header[:-1], b"body"
        chunks = [prefix[index : index + 8192] for index in range(0, len(prefix), 8192)]
        client = Mock()
        client.recv.side_effect = [*chunks, header[-1:] + body]
        self.assertEqual(_PROXY.receive_headers(client), (header, body))


class ImageBuildProxyTransportTests(unittest.TestCase):
    def setUp(self):
        name = self._testMethodName
        # silent-defaults: allow-contract -- DNS, TCP and relay probes require their own production primitive before injecting transport behavior; this chooses a prerequisite name, not a fallback implementation; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyTransportTests.test_dns_binds_device_and_shares_one_query_budget
        required = (
            "_query_ipv4"
            if name.startswith("test_dns_")
            else "resolve_ipv4_bound"
            if name == "test_expired_dns_budget_does_not_start_the_next_server" or name.startswith("test_tcp_")
            else "relay"
        )
        self.assertTrue(
            hasattr(_PROXY, required),
            msg=f"Interface-bound transport cannot prove {name}: provider_core.bound_http_proxy does not own {required}.",
        )
        self.assertTrue(
            hasattr(_PROXY, "time"),
            msg=f"Interface-bound transport cannot prove {name}: provider_core.bound_http_proxy does not own monotonic deadline accounting.",
        )

    def test_dns_binds_device_and_shares_one_query_budget(self):
        clock = [0]
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver
        resolver.connect.side_effect = lambda _address: clock.__setitem__(0, 0.2)
        resolver.send.side_effect = lambda _data: clock.__setitem__(0, 0.6)
        resolver.recv.return_value = _dns_response("example.invalid")
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.secrets, "randbits", return_value=111),
            patch.object(_PROXY.socket, "socket", return_value=resolver),
        ):
            addresses, aliases = _PROXY._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)
        self.assertEqual(addresses, {"example.invalid": ["203.0.113.1"]})
        self.assertEqual(aliases, {})
        resolver.setsockopt.assert_called_once_with(_PROXY.socket.SOL_SOCKET, _PROXY.socket.SO_BINDTODEVICE, b"eth0\0")
        resolver.connect.assert_called_once_with(("203.0.113.53", 53))
        self.assertEqual([call.args[0] for call in resolver.settimeout.call_args_list], [3, 2.8, 2.4])
        self.assertEqual(resolver.send.call_args.args[0][12:], _PROXY._dns_name("example.invalid") + struct.pack("!HH", 1, 1))

    def test_dns_groups_only_received_a_records_by_owner(self):
        """Keep repeated A records in one bucket without inventing absent owners."""
        for records, expected in (
            ((), {}),
            (
                (
                    ("example.invalid", "203.0.113.1"),
                    ("other.invalid", "203.0.113.2"),
                    ("example.invalid", "203.0.113.3"),
                ),
                {"example.invalid": ["203.0.113.1", "203.0.113.3"], "other.invalid": ["203.0.113.2"]},
            ),
        ):
            with self.subTest(records=records):
                response = (
                    struct.pack("!HHHHHH", 111, 0x8180, 1, len(records), 0, 0)
                    + _PROXY._dns_name("example.invalid")
                    + struct.pack("!HH", 1, 1)
                )
                for owner, address in records:
                    response += _PROXY._dns_name(owner) + struct.pack("!HHIH", 1, 1, 60, 4) + _PROXY.socket.inet_aton(address)
                resolver = MagicMock()
                resolver.__enter__.return_value = resolver
                resolver.recv.return_value = response
                with (
                    patch.object(_PROXY.time, "monotonic", return_value=0),
                    patch.object(_PROXY.secrets, "randbits", return_value=111),
                    patch.object(_PROXY.socket, "socket", return_value=resolver),
                ):
                    addresses, aliases = _PROXY._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)
                self.assertEqual(expected, addresses)
                self.assertEqual({}, aliases)

    def test_dns_rejects_a_reply_for_another_question(self):
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver
        resolver.recv.return_value = _dns_response("other.invalid")
        with (
            patch.object(_PROXY.time, "monotonic", return_value=0),
            patch.object(_PROXY.secrets, "randbits", return_value=111),
            patch.object(_PROXY.socket, "socket", return_value=resolver),
            self.assertRaises(OSError),
        ):
            _PROXY._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)

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
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.secrets, "randbits", return_value=111),
            patch.object(_PROXY.socket, "socket", return_value=resolver),
            self.assertRaises(TimeoutError),
        ):
            _PROXY._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)
        resolver.__exit__.assert_called_once()

    def test_dns_query_deadline_caps_wait_at_three_seconds(self):
        clock = [0]
        resolver = MagicMock()
        resolver.__enter__.return_value = resolver

        resolver.connect.side_effect = lambda _address: clock.__setitem__(0, 1)
        resolver.send.side_effect = lambda _data: clock.__setitem__(0, 3)
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.secrets, "randbits", return_value=111),
            patch.object(_PROXY.socket, "socket", return_value=resolver),
            self.assertRaises(TimeoutError),
        ):
            _PROXY._query_ipv4("example.invalid", "203.0.113.53", "eth0", deadline=30)
        self.assertEqual([call.args[0] for call in resolver.settimeout.call_args_list], [3, 2])
        resolver.recv.assert_not_called()
        self.assertEqual(3, clock[0])

    def test_expired_dns_budget_does_not_start_the_next_server(self):
        clock = [0]

        def expire(*args, **kwargs):
            clock[0] = 30
            raise TimeoutError("scripted resolver deadline")

        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY, "_dns_servers", return_value=("203.0.113.53", "203.0.113.54")),
            patch.object(_PROXY, "_query_ipv4", side_effect=expire) as query,
            self.assertRaises(TimeoutError),
        ):
            _PROXY.resolve_ipv4_bound("example.invalid", "eth0", deadline=30)
        self.assertEqual(1, query.call_count)

    def test_tcp_binds_device_and_expiry_closes_before_the_next_address(self):
        clock = [0]
        connection = Mock()

        def expire(_address):
            clock[0] = 30
            raise TimeoutError("scripted connect deadline")

        connection.connect.side_effect = expire
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY, "resolve_ipv4_bound", return_value=("203.0.113.1", "203.0.113.2")),
            patch.object(_PROXY.socket, "socket", return_value=connection) as created,
            self.assertRaises(TimeoutError),
        ):
            _PROXY.connect_bound("example.invalid", 443, "eth0")
        connection.setsockopt.assert_called_once_with(_PROXY.socket.SOL_SOCKET, _PROXY.socket.SO_BINDTODEVICE, b"eth0\0")
        connection.close.assert_called_once()
        created.assert_called_once()

    def test_tcp_tries_next_bound_address_after_an_ordinary_failure(self):
        clock = [0]
        first, second = Mock(), Mock()

        def fail_first(_address):
            clock[0] = 10
            raise OSError("scripted route failure")

        first.connect.side_effect = fail_first
        second.connect.side_effect = lambda _address: clock.__setitem__(0, 11)
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY, "resolve_ipv4_bound", return_value=("203.0.113.1", "203.0.113.2")),
            patch.object(_PROXY.socket, "socket", side_effect=(first, second)) as created,
        ):
            result = _PROXY.connect_bound("example.invalid", 443, "eth0")
        self.assertIs(result, second)
        self.assertEqual([call.args[0] for call in first.settimeout.call_args_list], [30])
        self.assertEqual([call.args[0] for call in second.settimeout.call_args_list], [20])
        for connection in (first, second):
            connection.setsockopt.assert_called_once_with(_PROXY.socket.SOL_SOCKET, _PROXY.socket.SO_BINDTODEVICE, b"eth0\0")
        first.connect.assert_called_once_with(("203.0.113.1", 443))
        second.connect.assert_called_once_with(("203.0.113.2", 443))
        first.close.assert_called_once()
        self.assertEqual(2, created.call_count)

    def test_tcp_late_success_closes_the_socket_instead_of_returning_it(self):
        """A late connection must fail while its socket still has an owner.

        This catches the inherited unconditional successful return at:
        https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L267-L272
        """
        clock = [0]
        connection = Mock()
        connection.connect.side_effect = lambda _address: clock.__setitem__(0, 30.1)
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY, "resolve_ipv4_bound", return_value=("203.0.113.1",)),
            patch.object(_PROXY.socket, "socket", return_value=connection),
            self.assertRaises(TimeoutError),
        ):
            _PROXY.connect_bound("example.invalid", 443, "eth0")
        connection.close.assert_called_once()

    def test_relay_resets_inactivity_only_after_a_completed_bounded_write(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                self._assert_relay_inactivity(blocked)

    def _assert_relay_inactivity(self, blocked):
        clock = [0]
        left, right = Mock(), Mock()
        left.recv.side_effect = lambda _size: (clock.__setitem__(0, 2), b"data")[1]

        def send(_data):
            # silent-defaults: allow-contract -- The blocked write reaches the original inactivity deadline; the completed write finishes at second three; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyTransportTests.test_relay_resets_inactivity_only_after_a_completed_bounded_write
            clock[0] = 120 if blocked else 3
            if blocked:
                raise TimeoutError("scripted write deadline")

        right.sendall.side_effect = send
        readiness = iter(([left], [], []))

        def select(_read, _write, _errors, timeout):
            ready = next(readiness)
            if not ready:
                clock[0] += timeout
            return ready, [], []

        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.select, "select", side_effect=select) as selected,
            self.assertRaises(TimeoutError),
        ):
            _PROXY.relay(left, right)
        right.settimeout.assert_called_once_with(118)
        # silent-defaults: allow-contract -- A blocked first write stops after one select; a completed write reaches the next select before inactivity expires; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyTransportTests.test_relay_resets_inactivity_only_after_a_completed_bounded_write
        self.assertEqual(selected.call_count, 1 if blocked else 2)
        # silent-defaults: allow-contract -- Only the completed write renews inactivity from second three, moving expiry from 120 to 123; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyTransportTests.test_relay_resets_inactivity_only_after_a_completed_bounded_write
        self.assertEqual(clock[0], 120 if blocked else 123)

    def test_client_half_close_preserves_the_returning_response(self):
        """Client EOF ends its upload, not the server's remaining response."""

        left, right = Mock(), Mock()
        clock, events, snapshots = [0], [], []

        def client_eof(_size):
            clock[0] = 1
            events.append("client EOF")
            return b""

        def server_read(_size):
            if clock[0] == 1:
                clock[0] = 2
                return b"response"
            clock[0] = 4
            events.append("server EOF")
            return b""

        def forward_response(_data):
            clock[0] = 3
            events.append("response forwarded")

        def select(read, _write, _errors, timeout):
            snapshots.append((list(read), timeout))
            # silent-defaults: allow-contract -- The scripted first read delivers client EOF, then server reads must still forward the response and its EOF; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyTransportTests.test_client_half_close_preserves_the_returning_response
            return ([left] if len(snapshots) == 1 else [right]), [], []

        left.recv.side_effect = client_eof
        right.recv.side_effect = server_read
        right.shutdown.side_effect = lambda _how: events.append("server write shutdown")
        left.sendall.side_effect = forward_response
        left.shutdown.side_effect = lambda _how: events.append("client write shutdown")
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.select, "select", side_effect=select),
        ):
            _PROXY.relay(left, right)
        self.assertEqual(
            events, ["client EOF", "server write shutdown", "response forwarded", "server EOF", "client write shutdown"]
        )
        self.assertEqual(snapshots, [([left, right], 120), ([right], 119), ([right], 120)])
        left.sendall.assert_called_once_with(b"response")
        right.shutdown.assert_called_once_with(_PROXY.socket.SHUT_WR)
        left.shutdown.assert_called_once_with(_PROXY.socket.SHUT_WR)

    def test_relay_late_success_cannot_renew_the_inactivity_budget(self):
        """A completed write after expiry ends relay without another select.

        This catches the inherited unconditional inactivity renewal at:
        https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L296-L298
        """
        clock = [0]
        left, right = Mock(), Mock()
        left.recv.side_effect = lambda _size: (clock.__setitem__(0, 1), b"data")[1]
        right.sendall.side_effect = lambda _data: clock.__setitem__(0, 120.1)
        with (
            patch.object(_PROXY.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(_PROXY.select, "select", return_value=([left], [], [])) as selected,
            self.assertRaises(TimeoutError),
        ):
            _PROXY.relay(left, right)
        right.sendall.assert_called_once_with(b"data")
        selected.assert_called_once()


class ImageBuildProxyWorkerTests(unittest.TestCase):
    def setUp(self):
        name = self._testMethodName
        # silent-defaults: allow-contract -- Capacity, readiness and forwarding probes require their corresponding production feature before injected worker behavior; no substitute feature is supplied; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_startup_failure_and_thread_completion_restore_capacity
        required = (
            "MAX_CONNECTIONS"
            if name in {"test_overload_does_not_start_a_thread", "test_startup_failure_and_thread_completion_restore_capacity"}
            else "require_interface"
            if name.startswith("test_readiness_")
            else "ProxyHandler"
        )
        self.assertTrue(
            hasattr(_PROXY, required), msg=f"Proxy worker cannot prove {name}: provider_core.bound_http_proxy does not own {required}."
        )

    def test_overload_does_not_start_a_thread(self):
        server = _server_without_listener()
        for _ in range(_PROXY.MAX_CONNECTIONS):
            self.assertTrue(server._slots.acquire())
        server.shutdown_request = Mock()
        request = Mock()
        with patch.object(_PROXY.socketserver.ThreadingTCPServer, "process_request") as start:
            server.process_request(request, ("127.0.0.1", 1))
        start.assert_not_called()
        server.shutdown_request.assert_called_once_with(request)

    def test_startup_failure_and_thread_completion_restore_capacity(self):
        for startup_failure in (False, True):
            with self.subTest(startup_failure=startup_failure):
                server = _server_without_listener()
                for _ in range(_PROXY.MAX_CONNECTIONS - 1):
                    self.assertTrue(server._slots.acquire())
                if startup_failure:
                    with (
                        patch.object(
                            _PROXY.socketserver.ThreadingTCPServer,
                            "process_request",
                            side_effect=RuntimeError("scripted startup"),
                        ),
                        self.assertRaises(RuntimeError),
                    ):
                        server.process_request(Mock(), ("127.0.0.1", 1))
                else:
                    self.assertTrue(server._slots.acquire())
                    with patch.object(_PROXY.socketserver.ThreadingTCPServer, "process_request_thread"):
                        server.process_request_thread(Mock(), ("127.0.0.1", 1))
                self.assertTrue(server._slots.acquire())
                self.assertFalse(server._slots.acquire())

    def test_private_pre_forwarding_error_has_fixed_http_and_safe_log(self):
        request = Mock()
        stderr = io.StringIO()
        with (
            patch.object(_PROXY, "receive_headers", return_value=(b"headers", b"")),
            patch.object(_PROXY, "parse_destination", side_effect=ValueError(_PRIVATE)),
            redirect_stderr(stderr),
        ):
            _PROXY.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="eth0"))
        request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        self.assertIn("request admission", stderr.getvalue())
        self.assertIn("ValueError", stderr.getvalue())
        self.assertNotIn(_PRIVATE, stderr.getvalue())

    def test_forwarding_error_does_not_append_http_failure_to_a_tunnel(self):
        request, connection = Mock(), MagicMock()
        connection.__enter__.return_value = connection
        stderr, stdout = io.StringIO(), io.StringIO()
        with (
            patch.object(_PROXY, "receive_headers", return_value=(b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n", b"")),
            patch.object(_PROXY, "connect_bound", return_value=connection),
            patch.object(_PROXY, "relay", side_effect=OSError(_PRIVATE)),
            redirect_stderr(stderr),
            redirect_stdout(stdout),
        ):
            _PROXY.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="eth0"))
        request.sendall.assert_called_once_with(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.assertNotIn(_PRIVATE, stderr.getvalue() + stdout.getvalue())

    def test_initial_forwarding_write_failure_does_not_append_http_failure(self):
        for phase in ("http_header", "connect_status", "connect_remainder"):
            with self.subTest(phase=phase):
                request, connection = Mock(), MagicMock()
                connection.__enter__.return_value = connection
                # silent-defaults: allow-contract -- The HTTP-header failure uses GET; the two CONNECT write failures use tunnel admission bytes; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_initial_forwarding_write_failure_does_not_append_http_failure
                header = (
                    b"GET http://example.invalid/ HTTP/1.1\r\n\r\n"
                    if phase == "http_header"
                    else b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n"
                )
                # silent-defaults: allow-contract -- Only the CONNECT-remainder scenario supplies body bytes so that its initial forwarding write is exercised; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_initial_forwarding_write_failure_does_not_append_http_failure
                remainder = b"body" if phase == "connect_remainder" else b""
                writer = request if phase == "connect_status" else connection
                writer.sendall.side_effect = OSError(_PRIVATE)
                stderr, stdout = io.StringIO(), io.StringIO()
                with (
                    patch.object(_PROXY, "receive_headers", return_value=(header, remainder)),
                    patch.object(_PROXY, "connect_bound", return_value=connection),
                    patch.object(_PROXY, "relay") as relay,
                    redirect_stderr(stderr),
                    redirect_stdout(stdout),
                ):
                    _PROXY.ProxyHandler(request, ("127.0.0.1", 1), SimpleNamespace(outbound_interface="eth0"))
                if phase == "http_header":
                    request.sendall.assert_not_called()
                else:
                    request.sendall.assert_called_once_with(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                relay.assert_not_called()
                self.assertNotIn(_PRIVATE, stderr.getvalue() + stdout.getvalue())
                self.assertIn("forwarding", stderr.getvalue())

    def test_readiness_is_private_and_cannot_overwrite_a_file_or_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            server = MagicMock(server_address=("127.0.0.1", 12345))
            _run_main(server, ready)
            self.assertEqual("12345\n", ready.read_text(encoding="ascii"))
            self.assertEqual(0o600, stat.S_IMODE(ready.stat().st_mode))
            for path in (ready, Path(temporary) / "symlink.port"):
                if path != ready:
                    path.symlink_to(ready)
                server.serve_forever.reset_mock()
                with self.subTest(path=path.name), self.assertRaises(SystemExit):
                    _run_main(server, path)
                server.serve_forever.assert_not_called()
                self.assertEqual("12345\n", ready.read_text(encoding="ascii"))

    def test_readiness_publication_exposes_only_complete_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            server = MagicMock(server_address=("127.0.0.1", 12345))
            link = _PROXY.os.link

            def inspect_publication(source, destination, **kwargs):
                self.assertFalse(ready.exists())
                self.assertEqual("12345\n", Path(source).read_text(encoding="ascii"))
                return link(source, destination, **kwargs)

            with patch.object(_PROXY.os, "link", side_effect=inspect_publication):
                _run_main(server, ready)
            self.assertEqual([], list(Path(temporary).glob(".proxy-ready.*")))

    def test_readiness_publication_failure_survives_temporary_cleanup_failure(self):
        """Temporary cleanup must not replace the failed publication outcome.

        This closes the inherited cleanup masking at:
        https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L406-L410
        """
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            ready.write_text("prior receipt\n", encoding="ascii")
            server = MagicMock(server_address=("127.0.0.1", 12345))
            stderr = io.StringIO()
            with (
                patch.object(_PROXY.os, "link", side_effect=FileExistsError(_PRIVATE)),
                patch.object(_PROXY.Path, "unlink", side_effect=PermissionError(_PRIVATE)),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as rejected,
            ):
                _run_main(server, ready)
            self.assertIn("readiness publication failed (FileExistsError)", str(rejected.exception))
            self.assertIn("Readiness publication did not complete", stderr.getvalue())
            self.assertIn("Cannot confirm temporary readiness cleanup", stderr.getvalue())
            self.assertNotIn(_PRIVATE, str(rejected.exception) + stderr.getvalue())
            self.assertEqual("prior receipt\n", ready.read_text(encoding="ascii"))
            retained = list(Path(temporary).glob(".proxy-ready.*"))
            self.assertEqual(1, len(retained))
            self.assertEqual("12345\n", retained[0].read_text(encoding="ascii"))
            server.serve_forever.assert_not_called()

    def test_readiness_published_receipt_still_serves_when_temporary_cleanup_fails(self):
        """A published receipt must stay usable, and replacement paths stay owned.

        This closes the inherited false creation failure at:
        https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L406-L410
        """
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                self._assert_published_readiness_cleanup_failure(replaced)

    def _assert_published_readiness_cleanup_failure(self, replaced):
        """Observe continued serving without ever unlinking a receipt path."""
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "proxy.port"
            server = MagicMock(server_address=("127.0.0.1", 12345))
            stderr = io.StringIO()
            link = _PROXY.os.link

            def publish(source, destination, **kwargs):
                link(source, destination, **kwargs)
                if replaced:
                    _PROXY.os.unlink(destination)
                    Path(destination).write_text("replacement receipt\n", encoding="ascii")

            with (
                patch.object(_PROXY.os, "link", side_effect=publish),
                patch.object(_PROXY.Path, "unlink", side_effect=PermissionError(_PRIVATE)),
                redirect_stderr(stderr),
            ):
                _run_main(server, ready)
            server.serve_forever.assert_called_once()
            self.assertIn("Readiness was published; serving will continue", stderr.getvalue())
            self.assertIn("Cannot confirm temporary readiness cleanup", stderr.getvalue())
            self.assertNotIn(_PRIVATE, stderr.getvalue())
            # silent-defaults: allow-contract -- The replacement scenario keeps its replacement receipt; the ordinary scenario keeps the published port receipt while serving continues; proof=tests/deployment/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_readiness_published_receipt_still_serves_when_temporary_cleanup_fails
            self.assertEqual(ready.read_text(encoding="ascii"), "replacement receipt\n" if replaced else "12345\n")
            retained = list(Path(temporary).glob(".proxy-ready.*"))
            self.assertEqual(1, len(retained))
            self.assertEqual("12345\n", retained[0].read_text(encoding="ascii"))
            self.assertEqual(0o600, stat.S_IMODE(retained[0].stat().st_mode))


def _dns_response(host):
    """Answer one fixed IPv4 question using a legal compressed owner name."""

    return (
        struct.pack("!HHHHHH", 111, 0x8180, 1, 1, 0, 0)
        + _PROXY._dns_name(host)
        + struct.pack("!HH", 1, 1)
        + b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, 60, 4)
        + b"\xcb\x00\x71\x01"
    )


def _server_without_listener():
    """Use the actual semaphore owner without opening a TCP listener."""

    with patch.object(_PROXY.socketserver.ThreadingTCPServer, "__init__", return_value=None):
        return _PROXY.ProxyServer(("127.0.0.1", 0), "eth0")


def _run_main(server, ready):
    """Inject the local startup-binding probe without opening a real socket."""
    with (
        patch.object(_PROXY, "ProxyServer") as factory,
        patch.object(_PROXY, "require_interface"),
        patch.object(_PROXY.socket, "socket") as probe_factory,
        patch.object(
            _PROXY.sys, "argv", ["image-build-proxy.py", "--interface", "eth0", "--port", "0", "--ready-file", str(ready)]
        ),
    ):
        factory.return_value.__enter__.return_value = server
        _PROXY.main()
    probe_factory.assert_called_once_with(_PROXY.socket.AF_INET, _PROXY.socket.SOCK_STREAM)
    probe = probe_factory.return_value.__enter__.return_value
    probe.setsockopt.assert_called_once_with(_PROXY.socket.SOL_SOCKET, _PROXY.socket.SO_BINDTODEVICE, b"eth0\0")
    probe.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
