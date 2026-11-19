"""HTTP proxy target failures retain the rejected port and its native cause."""

import unittest
from urllib.parse import urlsplit

from provider_core.validation.upstream.http_proxy import header_end, read_absolute_http_port, read_connect_authority


class ProxyTargetTests(unittest.TestCase):
    def test_malformed_ports_show_the_supplied_text_and_retain_conversion_errors(self):
        for read_port in (
            lambda: read_connect_authority("example.invalid:not-a-port"),
            lambda: read_absolute_http_port(urlsplit("http://example.invalid:not-a-port/")),
        ):
            with self.subTest(read_port=read_port), self.assertRaises(ValueError) as rejected:
                read_port()

            self.assertIn("port_text='not-a-port'", str(rejected.exception))
            self.assertIsInstance(rejected.exception.__cause__, ValueError)

    def test_large_numeric_port_evidence_is_bounded(self):
        with self.assertRaises(ValueError) as rejected:
            read_connect_authority("example.invalid:" + "9" * 1000)

        self.assertLess(len(str(rejected.exception)), 200)
        self.assertIn("MIN_PORT=1", str(rejected.exception))
        self.assertIn("MAX_PORT=65535", str(rejected.exception))

    def test_terminator_at_the_start_leaves_coalesced_body_outside_headers(self):
        self.assertIsNone(
            header_end(
                b"\r\n"
                b"\r",
            ),
        )
        self.assertEqual(
            header_end(
                b"\r\n"
                b"\r\n"
                b"body",
            ),
            4,
        )
