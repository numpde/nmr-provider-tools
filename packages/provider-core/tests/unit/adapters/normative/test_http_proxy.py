"""Proxy forwarding preserves CONNECT and converts absolute HTTP targets."""

import unittest
from http import HTTPStatus

from provider_core.adapters.http_proxy import CONNECT_ESTABLISHED_RESPONSE, closing_response, parse_destination


class ProxyRequestTests(unittest.TestCase):
    def test_connect_ports_are_admitted_before_socket_use(self):
        for port in ("0", "65536", "-1", "not-a-port"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                parse_destination(
                    (
                        f"CONNECT example.invalid:{port} HTTP/1.1\r\n"
                        f"\r\n"
                    ).encode(),
                )

        self.assertEqual(
            ("CONNECT", "example.invalid", 443),
            parse_destination(
                b"CONNECT example.invalid:443 HTTP/1.1\r\n"
                b"\r\n"
            )[:3]
        )

    def test_destination_port_boundaries_and_scheme_defaults_are_preserved(self):
        for port in (1, 65535):
            for (method, target) in (
                ("CONNECT", "example.invalid:" + str(port)),
                ("GET", "http://example.invalid:" + str(port) + "/path?query=1"),
            ):
                with self.subTest(method=method, port=port):
                    header = (
                        f"{method} {target} HTTP/1.1\r\n"
                        f"\r\n"
                    ).encode("ascii")

                    result = parse_destination(header)

                    self.assertEqual(result[:3], (method, "example.invalid", port))

                    self.assertEqual(
                        result[3],
                        header if method == "CONNECT" else (
                            b"GET /path?query=1 HTTP/1.1\r\n"
                            b"\r\n"
                        ),
                    )

        for (scheme, port) in (("http", 80), ("https", 443)):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    parse_destination(
                        (
                            f"GET {scheme}://example.invalid/ HTTP/1.1\r\n"
                            f"\r\n"
                        ).encode("ascii")
                    )[2],
                    port,
                )

    def test_empty_absolute_path_becomes_origin_slash(self):
        # RFC 9112 section 3.2.1 owns the path, including a query-only URI
        # https://www.rfc-editor.org/rfc/rfc9112.html#section-3.2.1
        for (suffix, origin) in (("", "/"), ("?q=one%20two", "/?q=one%20two")):
            with self.subTest(suffix=suffix):
                header = (
                    f"GET http://example.invalid{suffix} HTTP/1.1\r\n"
                    f"Host: example.invalid\r\n"
                    f"\r\n"
                ).encode("ascii")

                (method, host, port, forwarded) = parse_destination(header)

                self.assertEqual(("GET", "example.invalid", 80), (method, host, port))

                self.assertEqual(
                    f"GET {origin} HTTP/1.1\r\n"
                    f"Host: example.invalid\r\n"
                    f"\r\n".encode("ascii"),
                    forwarded,
                )

    def test_forwarding_preserves_headers_and_only_rewrites_the_request_target(self):
        header = (
            b"get https://example.invalid/path%20part?q=one%20two#fragment HTTP/1.0\r\n"
            b"Host: supplied.example\r\n"
            b"X-Custom: one\r\n"
            b"X-Custom: two\r\n"
            b"\r\n"
        )

        self.assertEqual(
            parse_destination(header),
            (
                "GET",
                "example.invalid",
                443,
                b"GET /path%20part?q=one%20two HTTP/1.0\r\n"
                b"Host: supplied.example\r\n"
                b"X-Custom: one\r\n"
                b"X-Custom: two\r\n"
                b"\r\n",
            ),
        )

    def test_replies_keep_the_existing_status_and_header_bytes(self):
        self.assertEqual(
            CONNECT_ESTABLISHED_RESPONSE,
            b"HTTP/1.1 200 Connection Established\r\n"
            b"\r\n",
        )

        self.assertEqual(
            closing_response(HTTPStatus.FORBIDDEN),
            b"HTTP/1.1 403 Forbidden\r\n"
            b"Connection: close\r\n"
            b"\r\n",
        )

        self.assertEqual(
            closing_response(HTTPStatus.BAD_GATEWAY),
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Connection: close\r\n"
            b"\r\n",
        )
