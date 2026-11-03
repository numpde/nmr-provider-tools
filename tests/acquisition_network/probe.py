"""Small, offline origin and isolated worker for the engine qualification."""
import http.server
import json
import os
from pathlib import Path
import socket
import sys


class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"proof")

    def log_message(self, *_arguments):
        pass


def exchange(proxy_port, origin_port, tunnel):
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=3) as client:
        if tunnel:
            client.sendall(f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
            response = b""
            while b"\r\n\r\n" not in response:
                block = client.recv(4096)
                if not block:
                    raise RuntimeError("CONNECT ended before its response")
                response += block
            if not response.startswith(b"HTTP/1.1 200 "):
                raise RuntimeError("CONNECT did not reach the gateway")
            client.sendall(b"GET /proof HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n")
        else:
            client.sendall(f"GET http://127.0.0.1:{origin_port}/proof HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n".encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while block := client.recv(4096):
            data += block
            if len(data) > 65536:
                raise RuntimeError("origin response exceeded its bound")
        if not data.endswith(b"\r\n\r\nproof"):
            raise RuntimeError("relay did not deliver the origin response")


def denied(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            if client.connect_ex(address) == 0:
                raise RuntimeError("direct external connection succeeded")
    except OSError:
        # Absence of an IPv6 stack also denies a direct connection.
        if family != socket.AF_INET6:
            raise


def check_routes():
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    if any(len(parts) >= 8 and parts[1] == "00000000" and parts[7] == "00000000"
           for parts in (row.split() for row in routes)):
        raise RuntimeError("IPv4 default route exists")
    path = Path("/proc/net/ipv6_route")
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split()
            # Linux emits a non-forwarding unreachable default route on lo.
            if len(parts) >= 10 and parts[0] == "0" * 32 and parts[1] == "00" and parts[-1] != "lo":
                raise RuntimeError("IPv6 default route exists")
    if set(os.listdir("/sys/class/net")) != {"lo"}:
        raise RuntimeError("worker namespace has a non-loopback interface")


if sys.argv[1] == "origin":
    with http.server.HTTPServer(("127.0.0.1", 0), Origin) as server:
        Path(sys.argv[2]).write_text(str(server.server_port) + "\n")
        server.serve_forever()
elif sys.argv[1] == "worker":
    check_routes()
    denied(socket.AF_INET, ("192.0.2.1", 443))
    if socket.has_ipv6:
        denied(socket.AF_INET6, ("2001:db8::1", 443, 0, 0))
    exchange(int(sys.argv[2]), int(sys.argv[3]), False)
    exchange(int(sys.argv[2]), int(sys.argv[3]), True)
    print(json.dumps({"netns": os.stat("/proc/self/ns/net").st_ino, "http": True, "connect": True}))
else:
    raise SystemExit("unknown fixture mode")
