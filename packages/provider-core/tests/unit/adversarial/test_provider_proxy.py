"""Unlisted destinations and unsafe socket grants refuse before outbound I/O."""

from provider_core.container_guard import require_container

require_container()

import io
import os
import socket
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from provider_core import bound_http_proxy, provider_proxy
from provider_core.provider_proxy import ServicePolicy, ServiceProxyHandler, ServiceProxyServer, ServiceRefused, socket_grant


class ServiceProxyRefusalTests(unittest.TestCase):
    def test_real_dns_decoder_reasons_reach_operator_without_private_packet_or_request_data(self):
        transaction = 17
        policy = ServicePolicy.admit("external", ["private.example:443"], [])
        question = bound_http_proxy._dns_name("other.private") + struct.pack("!HH", 1, 1)
        for packet, expected in (
            (struct.pack("!HHHHHH", transaction, 0x8003, 1, 0, 0, 0), "DNS server returned rcode 3"),
            (b"PRIVATE", "truncated DNS response"),
            (
                struct.pack("!HHHHHH", transaction, 0x8000, 1, 0, 0, 0) + question,
                "DNS response question does not match the request",
            ),
            (struct.pack("!HHHHHH", transaction, 0x8000, 1, 0, 0, 0) + b"\xc0", "truncated DNS compression pointer"),
        ):
            with self.subTest(reason=expected):
                udp = MagicMock()
                udp.__enter__.return_value = udp
                udp.recv.return_value = packet
                request = MagicMock()
                output = io.StringIO()
                with (
                    patch.object(
                        bound_http_proxy,
                        "receive_headers",
                        return_value=(b"CONNECT private.example:443 HTTP/1.1\r\nAuthorization: PRIVATE_KEY\r\n\r\n", b""),
                    ),
                    patch.object(bound_http_proxy, "_dns_servers", return_value=("192.0.2.53",)),
                    patch.object(bound_http_proxy.secrets, "randbits", return_value=transaction),
                    patch.object(bound_http_proxy.time, "monotonic", return_value=0),
                    patch.object(socket, "socket", return_value=udp) as channels,
                    redirect_stderr(output),
                ):
                    ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="eth0"))
                self.assertIn(expected, output.getvalue())
                for private in ("PRIVATE", "private.example", "other.private", "192.0.2.53"):
                    self.assertNotIn(private, output.getvalue())
                request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                channels.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
                udp.send.assert_called_once()
                udp.__exit__.assert_called_once()

    def test_denied_or_malformed_targets_never_reach_resolution_or_outbound_transport(self):
        policy = ServicePolicy.admit("external", ["api.example:443"], [])
        for header in (
            b"CONNECT private.example:443 HTTP/1.1\r\nAuthorization: PRIVATE\r\n\r\n",
            b"CONNECT api.example:80 HTTP/1.1\r\n\r\n",
            b"GET https://api.example HTTP/1.1\r\n\r\n",
            b"CONNECT user:PRIVATE@api.example:443 HTTP/1.1\r\n\r\n",
            b"CONNECT api.example:0 HTTP/1.1\r\n\r\n",
            b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
            b"CONNECT PRIVATE\xff HTTP/1.1\r\n\r\n",
        ):
            request = MagicMock()
            errors = io.StringIO()
            with (
                patch.object(bound_http_proxy, "receive_headers", return_value=(header, b"")),
                patch.object(bound_http_proxy, "connect_bound") as connect,
                patch.object(bound_http_proxy, "resolve_ipv4_bound") as resolve,
                redirect_stderr(errors),
            ):
                ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="lo"))
            connect.assert_not_called()
            resolve.assert_not_called()
            self.assertNotIn("PRIVATE", errors.getvalue())
            self.assertNotIn("private.example", errors.getvalue())
            self.assertIn("Cannot admit service proxy request:", errors.getvalue())
            self.assertNotIn(b"200", request.sendall.call_args.args[0])

    def test_policy_refuses_ambiguity_and_incomplete_or_extra_local_mappings(self):
        for mode, authorities, addresses in (
            ("external", [], []),
            ("external", ["api.example:443", "api.example:443"], []),
            ("external", ["API.example:443"], []),
            ("external", ["api.example:0443"], []),
            ("external", ["api.example:443"], ["api.example=192.0.2.1"]),
            ("local", ["api.example:443"], []),
            ("local", ["api.example:443"], ["api.example=192.0.2.1", "extra.example=192.0.2.2"]),
            ("local", ["api.example:443"], ["api.example=192.0.2.1", "api.example=192.0.2.2"]),
            ("local", ["api.example:443"], ["api.example=::1"]),
            ("local", ["192.0.2.1:443"], ["192.0.2.1=192.0.2.2"]),
            ("mapped", ["api.example:10443"], []),
            ("mapped", ["api.example:10443"], ["api.example=host-gateway"]),
            ("mapped", ["api.example:10443"], ["api.example=192.0.2.1", "extra.example=192.0.2.2"]),
        ):
            with self.subTest(mode=mode, authorities=authorities), self.assertRaises(ServiceRefused):
                ServicePolicy.admit(mode, authorities, addresses)

    def test_local_route_has_no_dns_fallback_when_the_frozen_address_fails(self):
        policy = ServicePolicy.admit("local", ["api.example:443"], ["api.example=192.0.2.1"])
        with patch.object(bound_http_proxy, "_dns_servers") as dns, patch.object(socket, "socket") as factory:
            factory.return_value.connect.side_effect = OSError("PRIVATE")
            with self.assertRaises(OSError):
                policy.connect("api.example", 443, "eth0")
        dns.assert_not_called()
        factory.return_value.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth0\0")
        factory.return_value.connect.assert_called_once_with(("192.0.2.1", 443))
        factory.return_value.close.assert_called_once()

    def test_mapped_route_keeps_connection_failure_without_dns_fallback(self):
        policy = ServicePolicy.admit("mapped", ["nmr.localhost:10443"], ["nmr.localhost=172.17.0.1"])
        with patch.object(bound_http_proxy, "_dns_servers") as dns, patch.object(socket, "socket") as factory:
            factory.return_value.connect.side_effect = OSError("PRIVATE")
            with self.assertRaises(OSError):
                policy.connect("nmr.localhost", 10443, "lo")
        dns.assert_not_called()
        factory.return_value.connect.assert_called_once_with(("172.17.0.1", 10443))
        factory.return_value.close.assert_called_once()

    def test_socket_and_readiness_grants_refuse_existing_leaves_or_public_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path, ready = root / "proxy.sock", root / "ready"
            with socket_grant(socket_path, ready):
                pass
            for leaf in (socket_path, ready):
                leaf.write_text("prior evidence")
                with self.assertRaises(ServiceRefused), socket_grant(socket_path, ready):
                    pass
                self.assertEqual(leaf.read_text(), "prior evidence")
                leaf.unlink()
                leaf.symlink_to(root / "absent")
                with self.assertRaises(ServiceRefused), socket_grant(socket_path, ready):
                    pass
                leaf.unlink()
            root.chmod(0o755)
            with self.assertRaises(ServiceRefused), socket_grant(socket_path, ready):
                pass

    def test_bind_failure_does_not_remove_a_prior_socket_or_file(self):
        policy = ServicePolicy.admit("external", ["api.example:443"], [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            path.write_text("prior evidence")
            with self.assertRaises(OSError):
                ServiceProxyServer(path, "lo", policy, status_directory=Path(directory))
            self.assertEqual(path.read_text(), "prior evidence")

    def test_failure_after_private_bind_restores_umask_and_closes_before_activation(self):
        policy = ServicePolicy.admit("external", ["api.example:443"], [])
        native_socket = socket.socket
        acquired = []

        def acquire(*args, **kwargs):
            channel = native_socket(*args, **kwargs)
            acquired.append(channel)
            return channel

        bind = bound_http_proxy.ProxyServer.server_bind

        def bind_then_fail(server):
            bind(server)
            self.assertEqual(Path(server.server_address).stat().st_mode & 0o777, 0o600)
            raise PermissionError(13, "PRIVATE")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            previous = os.umask(0)
            try:
                with (
                    patch.object(socket, "socket", side_effect=acquire),
                    patch.object(bound_http_proxy.ProxyServer, "server_bind", side_effect=bind_then_fail, autospec=True),
                    patch.object(ServiceProxyServer, "server_activate") as activate,
                    self.assertRaises(PermissionError),
                ):
                    ServiceProxyServer(path, "lo", policy, status_directory=Path(directory))
            finally:
                restored = os.umask(previous)
            self.assertEqual(restored, 0)
            activate.assert_not_called()
            self.assertEqual([channel.fileno() for channel in acquired], [-1])
            self.assertTrue(path.exists(), "Retain the created leaf for host inspection.")

    def test_invalid_interface_has_an_owned_diagnostic_before_socket_or_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path, ready = Path(directory) / "proxy.sock", Path(directory) / "ready"
            argv = [
                "provider_proxy",
                "--interface",
                "MISSING",
                "--mode",
                "external",
                "--authority",
                "api.example:443",
                "--socket",
                str(path),
                "--ready-file",
                str(ready),
                "--status-directory", directory, "--log-level", "INFO",
            ]
            with (
                patch.object(provider_proxy.sys, "argv", argv),
                patch.object(
                    bound_http_proxy,
                    "require_interface",
                    side_effect=ValueError("Cannot start proxy: the selected network interface does not exist."),
                ),
                patch.object(provider_proxy.socket, "socket") as channel,
                self.assertRaises(SystemExit) as caught,
            ):
                provider_proxy.main()
            self.assertEqual(str(caught.exception), "Cannot start proxy: the selected network interface does not exist.")
            channel.assert_not_called()
            self.assertFalse(path.exists())
            self.assertFalse(ready.exists())

    def test_filesystem_refusal_retains_safe_errno_and_private_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            path, ready = Path(directory) / "proxy.sock", Path(directory) / "ready"
            primary = PermissionError(13, "PRIVATE_PATH")
            with patch.object(Path, "lstat", side_effect=primary), self.assertRaises(ServiceRefused) as caught, socket_grant(path, ready):
                pass
            self.assertIs(caught.exception.__cause__, primary)
            self.assertIn("PermissionError; errno=13", str(caught.exception))
            self.assertNotIn("PRIVATE_PATH", str(caught.exception))

    def test_owned_dns_reasons_and_native_errno_reach_operator_without_private_dns_text(self):
        request = MagicMock()
        policy = ServicePolicy.admit("external", ["api.example:443"], [])
        for failure, expected in (
            (
                bound_http_proxy.ProxyResolutionFailed(
                    "Cannot resolve proxy destination: no non-loopback IPv4 DNS server is configured."
                ),
                "no non-loopback IPv4 DNS server",
            ),
            (
                bound_http_proxy.ProxyResolutionFailed(
                    "Cannot resolve proxy destination on the selected interface: the configured DNS queries failed.",
                    [TimeoutError("PRIVATE_DNS"), PermissionError(1, "PRIVATE_DEVICE")],
                ),
                "PermissionError; errno=1",
            ),
        ):
            errors = io.StringIO()
            with (
                patch.object(
                    bound_http_proxy, "receive_headers", return_value=(b"CONNECT api.example:443 HTTP/1.1\r\n\r\n", b"")
                ),
                patch.object(bound_http_proxy, "connect_bound", side_effect=failure),
                redirect_stderr(errors),
            ):
                ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="lo"))
            self.assertIn(expected, errors.getvalue())
            self.assertNotIn("PRIVATE", errors.getvalue())

    def test_dns_owner_keeps_safe_reason_and_original_query_failure_separate(self):
        primary = TimeoutError("PRIVATE_RESOLVER")
        with (
            patch.object(bound_http_proxy, "_dns_servers", return_value=("192.0.2.53",)),
            patch.object(bound_http_proxy, "_query_ipv4", side_effect=primary),
            patch.object(bound_http_proxy.time, "monotonic", return_value=0),
            self.assertRaises(bound_http_proxy.ProxyResolutionFailed) as caught,
        ):
            bound_http_proxy.resolve_ipv4_bound("PRIVATE_HOST", "eth0", deadline=30)
        self.assertEqual(caught.exception.failures, (primary,))
        self.assertIn("configured DNS queries failed", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_dns_deadline_keeps_prior_query_cause_without_starting_next_resolver(self):
        primary = OSError("PRIVATE_QUERY")
        clock = [0]

        def expire(*_args, **_kwargs):
            clock[0] = 31
            raise primary

        with (
            patch.object(bound_http_proxy, "_dns_servers", return_value=("192.0.2.53", "192.0.2.54")),
            patch.object(bound_http_proxy, "_query_ipv4", side_effect=expire) as query,
            patch.object(bound_http_proxy.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaises(bound_http_proxy.ProxyResolutionFailed) as caught,
        ):
            bound_http_proxy.resolve_ipv4_bound("PRIVATE_HOST", "eth0", deadline=30)
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertIs(caught.exception.failures[0], primary)
        self.assertIsInstance(caught.exception.failures[1], TimeoutError)
        self.assertIn("shared DNS deadline elapsed", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))
        query.assert_called_once_with("private_host", "192.0.2.53", "eth0", deadline=30)
