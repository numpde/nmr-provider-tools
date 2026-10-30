"""Service proxy admits explicit routes without changing destination TLS identity."""

from provider_core.container_guard import require_container

require_container()

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core import bound_http_proxy, provider_proxy
from provider_core.provider_proxy import ServicePolicy, ServiceProxyHandler, ServiceProxyServer


class ServiceProxyTests(unittest.TestCase):
    def test_live_settings_apply_atomic_replacement_and_keep_last_good_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            replacement = Path(directory) / "replacement.toml"
            path.write_text('[api_proxy]\nmax_connections = 2\nlog_level = "INFO"\n'
                            'allowed_authorities = ["api.example:443"]\n'
                            '[api_proxy.route]\naddresses = ["api.example=192.0.2.1"]\n')
            live = provider_proxy.LiveProxyConfiguration(path, "mapped")
            initial = live.read()
            server = SimpleNamespace(policy=initial.policy, _slots=bound_http_proxy.ConnectionAdmission(2))
            self.assertTrue(server._slots.acquire())
            self.assertTrue(server._slots.acquire())
            replacement.write_text('[api_proxy]\nmax_connections = 1\nlog_level = "DEBUG"\n'
                                   'allowed_authorities = ["other.example:443"]\n'
                                   '[api_proxy.route]\naddresses = ["other.example=192.0.2.2"]\n')
            replacement.replace(path)
            with patch.object(provider_proxy.logging, "getLogger") as logger:
                live.refresh(server)
                self.assertEqual(server.policy.authorities, frozenset({("other.example", 443)}))
                self.assertFalse(server._slots.acquire())
                server._slots.release()
                self.assertFalse(server._slots.acquire())
                logger.return_value.setLevel.assert_called_once_with("DEBUG")
                path.write_text("[api_proxy\n")
                live.next_poll = 0
                live.refresh(server)
                self.assertEqual(server.policy.authorities, frozenset({("other.example", 443)}))
                self.assertFalse(server._slots.acquire())
                logger.return_value.warning.assert_called_once()
                logger.return_value.warning.reset_mock()
                path.write_text('[api_proxy]\nmax_connections = 0\nlog_level = "INFO"\n'
                                'allowed_authorities = ["api.example:443"]\n')
                live.next_poll = 0
                live.refresh(server)
                self.assertIn("max_connections", str(logger.return_value.warning.call_args.args[1]))
                path.write_text('[api_proxy]\nmax_connections = ' + '[' * 2000 + '1' + ']' * 2000)
                live.next_poll = 0
                live.refresh(server)
                self.assertIn("deeply nested", str(logger.return_value.warning.call_args.args[1]))
                self.assertEqual(server.policy.authorities, frozenset({("other.example", 443)}))
            server._slots.release()
            self.assertTrue(server._slots.acquire())
            server._slots.release()
            with self.assertRaises(ValueError):
                live.read()

    def test_live_settings_refuse_fifo_without_blocking_the_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            os.mkfifo(path)
            live = provider_proxy.LiveProxyConfiguration(path, "external")
            with self.assertRaisesRegex(ValueError, "regular file"):
                live.read()

    def test_empty_live_authorities_refuse_new_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('[api_proxy]\nmax_connections = 2\nlog_level = "INFO"\n'
                            'allowed_authorities = []\n')
            settings = provider_proxy.LiveProxyConfiguration(path, "external").read()
            with self.assertRaises(provider_proxy.ServiceRefused):
                settings.policy.destination(b"CONNECT api.example:443 HTTP/1.1\r\n\r\n")
            path.write_text('[api_proxy]\nmax_connections = 2\nlog_level = "INFO"\n'
                            'allowed_authorities = []\n[api_proxy.route]\naddresses = ["invalid"]\n')
            with self.assertRaises(provider_proxy.ServiceRefused):
                provider_proxy.LiveProxyConfiguration(path, "external").read()

    def test_admitted_connection_uses_one_route_policy_across_reload(self):
        old = ServicePolicy.admit("mapped", ["api.example:443"], ["api.example=192.0.2.1"])
        new = ServicePolicy.admit("mapped", ["other.example:443"], ["other.example=192.0.2.2"])
        handler = object.__new__(ServiceProxyHandler)
        handler.server = SimpleNamespace(policy=old, outbound_interface="lo")
        handler.destination(b"CONNECT api.example:443 HTTP/1.1\r\n\r\n")
        handler.server.policy = new
        with patch.object(bound_http_proxy, "connect_bound") as connect:
            handler.connect("api.example", 443)
        connect.assert_called_once_with("192.0.2.1", 443, "lo")
        later = object.__new__(ServiceProxyHandler)
        later.server = handler.server
        with self.assertRaises(provider_proxy.ServiceRefused):
            later.destination(b"CONNECT api.example:443 HTTP/1.1\r\n\r\n")
        later.destination(b"CONNECT other.example:443 HTTP/1.1\r\n\r\n")
        with patch.object(bound_http_proxy, "connect_bound") as connect:
            later.connect("other.example", 443)
        connect.assert_called_once_with("192.0.2.2", 443, "lo")

    def test_entrypoint_logging_needs_no_scientific_binding_and_preserves_dependency_floor(self):
        script = """
import logging, sys
from provider_core.provider_proxy import _configure_proxy_logging
_configure_proxy_logging(sys.argv[1])
assert 'nmrnet_provider.runtime_logging' not in sys.modules
logging.getLogger('provider_core.proxy').info('proxy-info')
logging.getLogger('provider_core.proxy').debug('proxy-debug')
logging.getLogger('dependency').setLevel(logging.DEBUG)
logging.getLogger('dependency').info('dependency-info')
logging.getLogger('dependency').warning('dependency-warning')
"""
        for level in ("INFO", "DEBUG", "WARNING"):
            with self.subTest(level=level):
                result = subprocess.run([sys.executable, "-B", "-c", script, level],
                                        capture_output=True, text=True, timeout=5, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.count("proxy-info"), int(level != "WARNING"))
                self.assertEqual(result.stdout.count("proxy-debug"), int(level == "DEBUG"))
                self.assertNotIn("dependency-info", result.stdout)
                self.assertEqual(result.stdout.count("dependency-warning"), 1)

    def test_readiness_failure_closes_the_acquired_listener_before_constructor_raises(self):
        policy = ServicePolicy.admit("external", ["api.example:443"], [])
        closed = []
        close = ServiceProxyServer.server_close

        def observe_close(server):
            close(server)
            closed.append(server.socket.fileno())

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("provider_core.process_status.ProcessStatus.refresh", side_effect=OSError("authored status failure")),
                patch.object(ServiceProxyServer, "server_close", observe_close),
                self.assertRaises(OSError),
            ):
                ServiceProxyServer(Path(directory) / "proxy.sock", "lo", policy, status_directory=Path(directory))
        self.assertEqual(closed, [-1])

    def test_exclusive_proxy_restart_replaces_only_stale_private_endpoint_and_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path, ready = Path(directory) / "proxy.sock", Path(directory) / "ready"
            command = [sys.executable, "-B", "-c", "from provider_core.provider_proxy import main; main()",
                       "--interface", "lo", "--mode", "external", "--authority", "api.example:443",
                       "--socket", str(path), "--ready-file", str(ready), "--status-directory", directory,
                       "--log-level", "INFO"]

            def stop(child):
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=2)

            def wait_ready(child):
                deadline = time.monotonic() + 2
                while True:
                    self.assertIsNone(child.poll(), "Proxy exited before readiness")
                    try:
                        if ready.read_bytes() == b"ready\n":
                            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                                client.settimeout(1)
                                client.connect(str(path))
                                client.sendall(b"CONNECT denied.example:443 HTTP/1.1\r\n\r\n")
                                self.assertIn(b"403 Forbidden", client.recv(1024))
                            self.assertIsNone(child.poll())
                            return
                    # silent-defaults: allow-contract -- Only successful serving proves readiness while the prior stale endpoint is replaced.
                    except (FileNotFoundError, ConnectionRefusedError):
                        # Before admission, the prior dead listener can still
                        # have a filesystem leaf. Only serving proves readiness.
                        pass
                    if time.monotonic() >= deadline:
                        self.fail("Proxy did not replace its retired endpoint within the test budget")
                    time.sleep(0.01)

            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(stop, first)
            wait_ready(first)
            previous = path.stat().st_ino, ready.stat().st_ino
            contender = subprocess.run(command, capture_output=True, timeout=2)
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn(b"another proxy owns", contender.stderr)
            self.assertEqual(previous, (path.stat().st_ino, ready.stat().st_ino))
            self.assertIsNone(first.poll())
            first.kill()
            first.communicate(timeout=2)
            self.assertTrue(path.exists())
            second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(stop, second)
            wait_ready(second)

    def test_mapped_publication_preserves_authority_and_port_without_resolving_the_name(self):
        policy = ServicePolicy.admit("mapped", ["nmr.localhost:10443"], ["nmr.localhost=172.17.0.1"])
        self.assertEqual(
            policy.destination(b"CONNECT nmr.localhost:10443 HTTP/1.1\r\n\r\n")[:3],
            ("CONNECT", "nmr.localhost", 10443),
        )
        with patch.object(bound_http_proxy, "_dns_servers") as dns, patch.object(socket, "socket") as factory:
            policy.connect("nmr.localhost", 10443, "lo")
        dns.assert_not_called()
        factory.return_value.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0")
        factory.return_value.connect.assert_called_once_with(("172.17.0.1", 10443))

    def test_cli_publishes_private_complete_readiness_only_after_socket_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            path, ready = Path(directory) / "proxy.sock", Path(directory) / "ready"
            argv = [
                "provider_proxy",
                "--interface",
                "lo",
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

            def observe_ready(_server):
                self.assertEqual(ready.read_bytes(), b"ready\n")
                self.assertEqual(ready.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with patch.object(provider_proxy.sys, "argv", argv), patch.object(ServiceProxyServer, "serve_forever", observe_ready):
                provider_proxy.main()

    def test_local_mapping_selects_only_the_frozen_address_without_dns(self):
        policy = ServicePolicy.admit("local", ["api.example:443", "api.example:8443"], ["api.example=192.0.2.8"])
        sentinel = object()
        with patch.object(bound_http_proxy, "connect_bound", return_value=sentinel) as connect:
            self.assertIs(policy.connect("api.example", 443, "eth0"), sentinel)
        connect.assert_called_once_with("192.0.2.8", 443, "eth0")
        self.assertEqual(policy.destination(b"CONNECT api.example:8443 HTTP/1.1\r\n\r\n")[:3], ("CONNECT", "api.example", 8443))

    def test_external_route_preserves_the_admitted_host_for_bound_resolution(self):
        policy = ServicePolicy.admit("external", ["model.example:443"], [])
        with patch.object(bound_http_proxy, "connect_bound") as connect:
            policy.connect("model.example", 443, "wlan0")
        connect.assert_called_once_with("model.example", 443, "wlan0")

    def test_real_unix_listener_relays_bytes_and_preserves_client_half_close(self):
        policy = ServicePolicy.admit("local", ["api.example:443"], ["api.example=127.0.0.1"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            outbound, peer = socket.socketpair()
            with patch.object(bound_http_proxy, "connect_bound", return_value=outbound) as connect:
                with ServiceProxyServer(path, "lo", policy, status_directory=Path(directory)) as server:
                    worker = threading.Thread(target=server.handle_request)
                    worker.start()
                    try:
                        with socket.socket(socket.AF_UNIX) as client, peer:
                            client.settimeout(2)
                            peer.settimeout(2)
                            client.connect(str(path))
                            client.sendall(b"CONNECT api.example:443 HTTP/1.1\r\n\r\nopaque-prefix")
                            self.assertEqual(client.recv(4096), b"HTTP/1.1 200 Connection Established\r\n\r\n")
                            self.assertEqual(peer.recv(4096), b"opaque-prefix")
                            client.shutdown(socket.SHUT_WR)
                            self.assertEqual(peer.recv(4096), b"")
                            peer.sendall(b"opaque-response")
                            peer.shutdown(socket.SHUT_WR)
                            self.assertEqual(client.recv(4096), b"opaque-response")
                            self.assertEqual(client.recv(4096), b"")
                    finally:
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                connect.assert_called_once_with("127.0.0.1", 443, "lo")
