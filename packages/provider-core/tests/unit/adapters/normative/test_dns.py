"""DNS names use length-prefixed IDNA labels and a terminating root label."""

import unittest

from provider_core.adapters.dns import encode_dns_name


class DNSNameEncodingTests(unittest.TestCase):
    def test_idna_name_has_exact_wire_framing(self):
        self.assertEqual(encode_dns_name("bücher.example."), b"\x0dxn--bcher-kva\x07example\x00")
