"""DNS names use length-prefixed IDNA labels and a terminating root label."""

import unittest

from provider_core.adapters.dns import encode_dns_name, ipv4_query


class DNSNameEncodingTests(unittest.TestCase):
    def test_idna_name_has_exact_wire_framing(self):
        self.assertEqual(encode_dns_name("bücher.example."), b"\x0dxn--bcher-kva\x07example\x00")

    def test_ipv4_query_has_one_recursive_in_a_question(self):
        self.assertEqual(
            ipv4_query("bücher.example.", 17),
            b"\x00\x11\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x0dxn--bcher-kva\x07example\x00\x00\x01\x00\x01",
        )
