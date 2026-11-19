"""The supported DNS name profile preserves IDNA and its original byte bound."""

import unittest

from provider_core.validation.upstream.dns import read_dns_labels


class DNSNameTests(unittest.TestCase):
    def test_idna_and_trailing_root_dot_preserve_label_bytes(self):
        self.assertEqual(read_dns_labels("bücher.example."), [b"xn--bcher-kva", b"example"])

    def test_name_limit_counts_the_supplied_root_dot(self):
        hostname = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))

        self.assertEqual(b".".join(read_dns_labels(hostname)).decode("ascii"), hostname)

        with self.assertRaisesRegex(ValueError, "name_byte_count=254.*max_name_bytes=253"):
            read_dns_labels(hostname + ".")

    def test_empty_name_and_invalid_idna_labels_are_rejected(self):
        with self.assertRaises(ValueError):
            read_dns_labels("")

        for hostname in ("a" * 64 + ".example", "a..example"):
            with self.subTest(hostname=hostname), self.assertRaises(UnicodeError):
                read_dns_labels(hostname)
