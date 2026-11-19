"""Unlisted destinations and unsafe socket grants refuse before outbound I/O."""

from provider_core.container_guard import require_container

require_container()

import io
import os
import socket
import struct
import tempfile
import unittest
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from provider_core import bound_http_proxy, provider_proxy
from provider_core.provider_proxy import ServicePolicy, ServiceProxyHandler, ServiceProxyServer, ServiceRefused, socket_grant
from tests.fakes.proxy_logs import capture_proxy_logs


class ServiceProxyRefusalTests(unittest.TestCase):
    def _dns_decoder_refusals(self):
        transaction = 17
        policy = ServicePolicy.admit("external", ["private.example:443"], [])
        question = bound_http_proxy._dns_name("other.private") + struct.pack("!HH", 1, 1)

        for (packet, expected) in (
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
                        return_value=(
                            (
                                b"CONNECT private.example:443 HTTP/1.1\r\n"
                                b"Authorization: PRIVATE_KEY\r\n"
                                b"\r\n"
                            ),
                            b""
                        ),
                    ),
                    patch.object(bound_http_proxy, "_dns_servers", return_value=("192.0.2.53",)),
                    patch.object(bound_http_proxy.secrets, "randbits", return_value=transaction),
                    patch.object(bound_http_proxy.time, "monotonic", return_value=0),
                    patch.object(socket, "socket", return_value=udp) as channels,
                    capture_proxy_logs(output),
                ):
                    ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="eth0"))

                yield expected, output.getvalue(), request, channels, udp

    def test_real_dns_decoder_reasons_reach_operator(self):
        for (expected, output, request, channels, udp) in self._dns_decoder_refusals():
            self.assertIn(expected, output)
            self.assertIn("192.0.2.53", output)

            request.sendall.assert_called_once_with(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n"
                b"\r\n",
            )

            channels.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
            udp.send.assert_called_once()
            udp.__exit__.assert_called_once()

    @unittest.skip("Privacy policy under review")
    def test_dns_decoder_logs_omit_packet_and_request_data(self):
        for (_expected, output, _request, _channels, _udp) in self._dns_decoder_refusals():
            for private in ("PRIVATE", "private.example", "other.private"):
                self.assertNotIn(private, output)

    def _denied_targets(self):
        policy = ServicePolicy.admit("external", ["api.example:443"], [])

        for header in (
            b"CONNECT private.example:443 HTTP/1.1\r\n"
            b"Authorization: PRIVATE\r\n"
            b"\r\n",
            b"CONNECT api.example:80 HTTP/1.1\r\n"
            b"\r\n",
            b"GET https://api.example HTTP/1.1\r\n"
            b"\r\n",
            b"CONNECT user:PRIVATE@api.example:443 HTTP/1.1\r\n"
            b"\r\n",
            b"CONNECT api.example:0 HTTP/1.1\r\n"
            b"\r\n",
            b"CONNECT [::1]:443 HTTP/1.1\r\n"
            b"\r\n",
            b"CONNECT PRIVATE\xff HTTP/1.1\r\n"
            b"\r\n",
        ):
            request = MagicMock()
            errors = io.StringIO()

            with (
                patch.object(bound_http_proxy, "receive_headers", return_value=(header, b"")),
                patch.object(bound_http_proxy, "connect_bound") as connect,
                patch.object(bound_http_proxy, "resolve_ipv4_bound") as resolve,
                capture_proxy_logs(errors),
            ):
                ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="lo"))

            yield connect, resolve, errors.getvalue(), request

    def test_denied_or_malformed_targets_never_reach_resolution_or_outbound_transport(self):
        for (connect, resolve, errors, request) in self._denied_targets():
            connect.assert_not_called()
            resolve.assert_not_called()
            self.assertIn("Cannot admit service proxy request:", errors)
            self.assertNotIn(b"200", request.sendall.call_args.args[0])

    @unittest.skip("Privacy policy under review")
    def test_denied_target_logs_omit_request_data(self):
        for (_connect, _resolve, errors, _request) in self._denied_targets():
            self.assertNotIn("PRIVATE", errors)
            self.assertNotIn("private.example", errors)

    def test_policy_refuses_ambiguity_and_incomplete_or_extra_local_mappings(self):
        for (mode, authorities, addresses) in (
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
            root = pathlib.Path(directory)
            (socket_path, ready) = (root / "proxy.sock", root / "ready")

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
            proxy_socket_path = pathlib.Path(directory) / "proxy.sock"

            proxy_socket_path.write_text("prior evidence")

            with self.assertRaises(OSError):
                ServiceProxyServer(proxy_socket_path, "lo", policy, status_directory=pathlib.Path(directory))

            self.assertEqual(proxy_socket_path.read_text(), "prior evidence")

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
            self.assertEqual(pathlib.Path(server.server_address).stat().st_mode & 0o777, 0o600)
            raise PermissionError(13, "PRIVATE")

        with tempfile.TemporaryDirectory() as directory:
            proxy_socket_path = pathlib.Path(directory) / "proxy.sock"
            previous = os.umask(0)

            try:
                with (
                    patch.object(socket, "socket", side_effect=acquire),
                    patch.object(bound_http_proxy.ProxyServer, "server_bind", side_effect=bind_then_fail, autospec=True),
                    patch.object(ServiceProxyServer, "server_activate") as activate,
                    self.assertRaises(PermissionError),
                ):
                    ServiceProxyServer(proxy_socket_path, "lo", policy, status_directory=pathlib.Path(directory))
            finally:
                restored = os.umask(previous)

            self.assertEqual(restored, 0)
            activate.assert_not_called()
            self.assertEqual([channel.fileno() for channel in acquired], [-1])
            self.assertTrue(proxy_socket_path.exists(), "Retain the created leaf for host inspection.")

    def test_invalid_interface_has_an_owned_diagnostic_before_socket_or_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            (path, ready) = (pathlib.Path(directory) / "proxy.sock", pathlib.Path(directory) / "ready")

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
                "--status-directory",
                directory,
                "--log-level",
                "INFO",
            ]

            output = io.StringIO()

            with (
                capture_proxy_logs(output),
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

            self.assertEqual(caught.exception.code, 1)
            self.assertIn("Cannot start proxy: the selected network interface does not exist.", output.getvalue())
            channel.assert_not_called()
            self.assertFalse(path.exists())
            self.assertFalse(ready.exists())

    def _filesystem_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            (path, ready) = (pathlib.Path(directory) / "proxy.sock", pathlib.Path(directory) / "ready")
            primary = PermissionError(13, "PRIVATE_PATH")

            with patch.object(pathlib.Path, "lstat", side_effect=primary), self.assertRaises(ServiceRefused) as caught, socket_grant(path, ready):
                pass

            return caught.exception, primary

    def test_filesystem_refusal_retains_errno_and_cause(self):
        refusal, primary = self._filesystem_refusal()

        self.assertIs(refusal.__cause__, primary)
        self.assertIn("PermissionError; errno=13", str(refusal))

    @unittest.skip("Privacy policy under review")
    def test_filesystem_refusal_omits_path(self):
        refusal, _primary = self._filesystem_refusal()

        self.assertNotIn("PRIVATE_PATH", str(refusal))

    def test_owned_dns_reasons_and_original_query_causes_reach_operator(self):
        request = MagicMock()
        policy = ServicePolicy.admit("external", ["api.example:443"], [])

        for (failure, expected) in (
            (
                bound_http_proxy.ProxyResolutionFailed(
                    "Cannot resolve proxy destination: no non-loopback IPv4 DNS server is configured."
                ),
                "no non-loopback IPv4 DNS server",
            ),
            (
                bound_http_proxy.ProxyResolutionFailed(
                    "Cannot resolve proxy destination on the selected interface: the configured DNS queries failed.",
                    [TimeoutError("DNS query deadline elapsed"), PermissionError(1, "Device binding denied")],
                ),
                "PermissionError: [Errno 1] Device binding denied",
            ),
        ):
            errors = io.StringIO()

            with (
                patch.object(
                    bound_http_proxy,
                    "receive_headers",
                    return_value=(
                        (
                            b"CONNECT api.example:443 HTTP/1.1\r\n"
                            b"\r\n"
                        ),
                        b""
                    )
                ),
                patch.object(bound_http_proxy, "connect_bound", side_effect=failure),
                capture_proxy_logs(errors),
            ):
                ServiceProxyHandler(request, "", SimpleNamespace(policy=policy, outbound_interface="lo"))

            self.assertIn(expected, errors.getvalue())

            if failure.failures:
                self.assertIn("DNS query deadline elapsed", errors.getvalue())
                self.assertIn("Device binding denied", errors.getvalue())

    def _dns_query_refusal(self):
        primary = TimeoutError("PRIVATE_RESOLVER")

        with (
            patch.object(bound_http_proxy, "_dns_servers", return_value=("192.0.2.53",)),
            patch.object(bound_http_proxy, "_query_ipv4", side_effect=primary),
            patch.object(bound_http_proxy.time, "monotonic", return_value=0),
            self.assertRaises(bound_http_proxy.ProxyResolutionFailed) as caught,
        ):
            bound_http_proxy.resolve_ipv4_bound("PRIVATE_HOST", "eth0", deadline=30)

        return caught.exception, primary

    def test_dns_owner_keeps_reason_and_original_query_failure(self):
        refusal, primary = self._dns_query_refusal()

        self.assertEqual(refusal.failures, (primary,))
        self.assertIn("configured DNS queries failed", str(refusal))

    @unittest.skip("Privacy policy under review")
    def test_dns_query_refusal_omits_query_data(self):
        refusal, _primary = self._dns_query_refusal()

        self.assertNotIn("PRIVATE", str(refusal))

    def _dns_deadline_refusal(self):
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

        return caught.exception, primary, query

    def test_dns_deadline_keeps_prior_query_cause_without_starting_next_resolver(self):
        refusal, primary, query = self._dns_deadline_refusal()

        self.assertIsInstance(refusal, TimeoutError)
        self.assertIs(refusal.failures[0], primary)
        self.assertIsInstance(refusal.failures[1], TimeoutError)
        self.assertIn("shared DNS deadline elapsed", str(refusal))
        query.assert_called_once_with("private_host", "192.0.2.53", "eth0", deadline=30)

    @unittest.skip("Privacy policy under review")
    def test_dns_deadline_refusal_omits_query_data(self):
        refusal, _primary, _query = self._dns_deadline_refusal()

        self.assertNotIn("PRIVATE", str(refusal))
