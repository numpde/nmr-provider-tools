"""Exercise acquisition requests through a real upstream SOCKS connection."""

from contextlib import contextmanager
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest

from acquisition_tool.gateway import UpstreamGateway, upstream_proxy


class SocksOrigin(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.rfile.read(2)
        self.rfile.read(1)
        self.wfile.write(b"\x05\x00")
        header = self.rfile.read(4)
        if header[3] == 3:
            host = self.rfile.read(self.rfile.read(1)[0]).decode()
        else:
            host = socket.inet_ntoa(self.rfile.read(4))
        port = int.from_bytes(self.rfile.read(2), "big")
        self.server.destinations.append((host, port))
        self.wfile.write(bytes([5, self.server.reply, 0, 1]) + b"\x7f\x00\x00\x01\x00\x50")
        if not self.server.reply:
            self.server.request = self.rfile.readline()
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\nConnection: close\r\n\r\nfrom-chosen-proxy!")


@contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


class AcquisitionProxyTests(unittest.TestCase):
    def request(self, upstream, host, *, tunnel=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sock"
            with serving(UpstreamGateway(path, upstream)):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    if tunnel:
                        client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
                        self.assertEqual(b"200", client.recv(4096).split()[1])
                    path = "/artifact" if tunnel else f"http://{host}/artifact"
                    client.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                    chunks = []
                    while part := client.recv(4096):
                        chunks.append(part)
                    return b"".join(chunks)

    def test_request_uses_selected_socks_proxy_and_remote_dns(self):
        with serving(socketserver.ThreadingTCPServer(("127.0.0.1", 0), SocksOrigin)) as proxy:
            proxy.destinations, proxy.reply = [], 0
            response = self.request(upstream_proxy(f"socks5h://127.0.0.1:{proxy.server_address[1]}"), "only-upstream.invalid", tunnel=True)
            self.assertIn(b"from-chosen-proxy!", response)
            self.assertEqual([("only-upstream.invalid", 443)], proxy.destinations)
            self.assertEqual(b"GET /artifact HTTP/1.1\r\n", proxy.request)

    def test_upstream_refusal_does_not_fall_back_to_reachable_origin(self):
        with socket.socket() as origin:
            origin.bind(("127.0.0.1", 0))
            origin.listen()
            origin.settimeout(0.1)
            with serving(socketserver.ThreadingTCPServer(("127.0.0.1", 0), SocksOrigin)) as proxy:
                proxy.destinations, proxy.reply = [], 5
                with self.assertLogs("provider_core.proxy", level="ERROR") as logs:
                    response = self.request(upstream_proxy(f"socks5h://127.0.0.1:{proxy.server_address[1]}"), f"127.0.0.1:{origin.getsockname()[1]}")
                self.assertIn(b"502", response)
                self.assertIn("ProxyError", " ".join(logs.output))
                with self.assertRaises(TimeoutError):
                    origin.accept()
