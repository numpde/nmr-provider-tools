"""Expose one explicit outbound route through the acquisition Unix gateway.

Interface routing remains owned by bound_http_proxy. For an upstream proxy,
python-socks owns negotiation; the existing HTTP handler owns forwarding,
limits and diagnostics. No failed proxy connection falls back to direct access.
"""

import argparse
import os
import logging
from pathlib import Path
import sys
from urllib.parse import urlsplit

from provider_core import bound_http_proxy


def upstream_proxy(url):
    from python_socks.sync import Proxy

    endpoint = urlsplit(url)
    if (
        endpoint.scheme not in {"http", "socks5", "socks5h"} or
        not endpoint.hostname or not endpoint.port or
        endpoint.username is not None or endpoint.password is not None or
        endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment
    ):
        raise ValueError("Acquisition proxy must be an http, socks5 or socks5h host:port URL without credentials or a path")
    # socks5h delegates destination DNS to the proxy; socks5 resolves locally.
    normalized = endpoint._replace(scheme="socks5", path="").geturl() if endpoint.scheme == "socks5h" else url
    return Proxy.from_url(normalized, rdns=endpoint.scheme != "socks5")


class UpstreamHandler(bound_http_proxy.ProxyHandler):
    def connect(self, host, port):
        from python_socks import ProxyError

        try:
            return self.server.upstream.connect(host, port, timeout=bound_http_proxy.CONNECT_SECONDS)
        except ProxyError as error:
            # The HTTP boundary reports connection failures as 502 and logs causes.
            raise OSError("The selected upstream proxy refused or interrupted acquisition") from error


class UpstreamGateway(bound_http_proxy.UnixProxyServer):
    request_handler = UpstreamHandler

    def __init__(self, address, upstream):
        self.upstream = upstream
        super().__init__(address, "system")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True, help="system, a host interface, or an upstream HTTP/SOCKS proxy URL")
    parser.add_argument("--unix-socket", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--resolver-file", required=True, type=Path)
    args = parser.parse_args()
    if "://" not in args.route:
        os.execv(sys.executable, [
            sys.executable, "-P", "-B", "-m", "provider_core.bound_http_proxy",
            "--interface", args.route, "--unix-socket", str(args.unix_socket),
            "--ready-file", str(args.ready_file), "--resolver-file", str(args.resolver_file),
        ])
    handler = logging.StreamHandler()
    handler.setFormatter(bound_http_proxy.CredentialRedactingFormatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    upstream = upstream_proxy(args.route)
    with UpstreamGateway(args.unix_socket, upstream) as server:
        bound_http_proxy.publish_readiness(args.ready_file, f"{server.server_address}\n")
        server.serve_forever()


if __name__ == "__main__":
    main()
