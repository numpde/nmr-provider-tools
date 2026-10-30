"""Actual service proxy carries verified destination TLS without DNS fallback.

This is a network-none, loopback component proof. It does not qualify the host
launcher, named peer network, external interface, or physical relay retirement.
"""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from tests.fakes.tls_peer import TlsPeer

from provider_core.transport import HttpTransport
from provider_core import bound_http_proxy
from provider_core.provider_proxy import LiveProxyConfiguration, ServicePolicy, ServiceProxyServer


class ActualServiceProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_reload_changes_new_tunnels_without_interrupting_existing_tunnel(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        peer = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = peer.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.toml"
            replacement = root / "replacement.toml"

            def configuration(host):
                return (f'[api_proxy]\nmax_connections = 4\nlog_level = "INFO"\n'
                        f'allowed_authorities = ["{host}:{port}"]\n'
                        f'[api_proxy.route]\naddresses = ["{host}=127.0.0.1"]\n')

            settings_path.write_text(configuration("first.example"))
            live = LiveProxyConfiguration(settings_path, "mapped")
            initial = live.read()
            with ServiceProxyServer(root / "proxy.sock", "lo", initial.policy, status_directory=root,
                                    live_configuration=live) as server:
                live.apply(server, initial)
                listener = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                listener.start()
                writers = []
                try:
                    reader, writer = await asyncio.open_unix_connection(server.server_address)
                    writers.append(writer)
                    writer.write(f"CONNECT first.example:{port} HTTP/1.1\r\n\r\n".encode())
                    await writer.drain()
                    self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
                    replacement.write_text(configuration("second.example"))
                    replacement.replace(settings_path)
                    live.next_poll = 0
                    async with asyncio.timeout(2):
                        while ("second.example", port) not in server.policy.authorities:
                            await asyncio.sleep(0.01)
                    writer.write(b"existing tunnel")
                    await writer.drain()
                    self.assertEqual(await asyncio.wait_for(reader.readexactly(15), 2), b"existing tunnel")

                    refused_reader, refused_writer = await asyncio.open_unix_connection(server.server_address)
                    writers.append(refused_writer)
                    refused_writer.write(f"CONNECT first.example:{port} HTTP/1.1\r\n\r\n".encode())
                    await refused_writer.drain()
                    self.assertIn(b"403", await asyncio.wait_for(refused_reader.readuntil(b"\r\n\r\n"), 2))
                    new_reader, new_writer = await asyncio.open_unix_connection(server.server_address)
                    writers.append(new_writer)
                    new_writer.write(f"CONNECT second.example:{port} HTTP/1.1\r\n\r\n".encode())
                    await new_writer.drain()
                    self.assertIn(b"200", await asyncio.wait_for(new_reader.readuntil(b"\r\n\r\n"), 2))
                finally:
                    for writer in writers:
                        writer.close()
                    for writer in writers:
                        await writer.wait_closed()
                    await asyncio.to_thread(server.shutdown)
                    await asyncio.to_thread(listener.join, 2)
                    peer.close()
                    await peer.wait_closed()

    async def test_frozen_local_destination_keeps_original_tls_name_headers_and_body(self):
        async with TlsPeer("success") as peer:
            port = urlsplit(peer.origin).port
            authority = f"localhost:{port}"
            policy = ServicePolicy.admit("local", [authority], ["localhost=127.0.0.1"])
            finished = threading.Event()
            workers = []

            class ObservedProxy(ServiceProxyServer):
                def process_request_thread(self, request, client_address):
                    workers.append(threading.current_thread())
                    try:
                        super().process_request_thread(request, client_address)
                    finally:
                        finished.set()

            with (
                tempfile.TemporaryDirectory() as directory,
                ObservedProxy(Path(directory) / "proxy.sock", "lo", policy, status_directory=Path(directory)) as server,
            ):
                listener = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                listener.start()
                transport = HttpTransport(
                    trust=peer.trust, exchange_seconds=2, retirement_seconds=1, proxy_socket=server.server_address
                )
                try:
                    with patch.object(bound_http_proxy, "_dns_servers", side_effect=AssertionError("local route attempted DNS")):
                        response = await transport.exchange(
                            httpx.Request(
                                "POST",
                                f"https://{authority}/exact?query=1",
                                headers={"Authorization": "Bearer PRIVATE_KEY"},
                                content=b"PRIVATE_BODY",
                            ),
                            maximum_bytes=4,
                        )
                    self.assertEqual(response.body, b"data")
                    self.assertEqual(peer.server_names, ["localhost"])
                    self.assertEqual(len(peer.requests), 1)
                    self.assertIn(b"POST /exact?query=1 HTTP/1.1\r\n", peer.requests[0])
                    self.assertIn(f"Host: {authority}\r\n".encode(), peer.requests[0])
                    self.assertIn(b"Authorization: Bearer PRIVATE_KEY\r\n", peer.requests[0])
                    self.assertEqual(peer.request_bodies, [b"PRIVATE_BODY"])
                finally:
                    try:
                        await transport.retire()
                        completed = await asyncio.to_thread(finished.wait, 2)
                    finally:
                        await asyncio.to_thread(server.shutdown)
                        await asyncio.to_thread(listener.join, 2)
                        for worker in workers:
                            await asyncio.to_thread(worker.join, 2)
                    self.assertTrue(completed)
                    self.assertFalse(listener.is_alive())
                    self.assertTrue(all(not worker.is_alive() for worker in workers))
