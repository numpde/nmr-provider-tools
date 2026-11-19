"""The supported DNS name profile preserves IDNA and its original byte bound."""

import struct
import unittest

from provider_core.validation.upstream.dns import DNSNameRejected, DNSResponseRejected, read_dns_labels, read_dns_name, read_ipv4_response


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

    def test_dns_name_depth_starts_at_zero_and_bounds_compression(self):
        encoded_name = b"\x03WWW\x07Example\x03COM\0"
        message = encoded_name + b"\x03API\xc0\x04"

        for (offset, expected) in (
            (0, ("www.example.com", len(encoded_name))),
            (len(encoded_name), ("api.example.com", len(message))),
        ):
            with self.subTest(offset=offset):
                self.assertEqual(expected, read_dns_name(message, offset, max_pointer_depth=16))
                self.assertEqual(expected, read_dns_name(message, offset, 0, max_pointer_depth=16))

        self.assertEqual(("www.example.com", len(encoded_name)), read_dns_name(encoded_name, 0, 16, max_pointer_depth=16))

        for (encoded, offset, depth) in ((message, len(encoded_name), 16), (b"", 0, 17)):
            with (
                self.subTest(offset=offset, depth=depth),
                self.assertRaisesRegex(DNSNameRejected, "depth=17 should be at most max_pointer_depth=16"),
            ):
                read_dns_name(encoded, offset, depth, max_pointer_depth=16)

        with self.assertRaisesRegex(DNSNameRejected, "depth=17 should be at most max_pointer_depth=16"):
            read_dns_name(b"\xc0\x00", 0, max_pointer_depth=16)

    def test_malformed_names_report_offsets_and_preserve_decode_causes(self):
        for (message, expected) in (
            (b"", "offset=0 should be below message_byte_count=0"),
            (b"\xc0", "pointer_end=2 should be at most message_byte_count=1"),
            (b"\x03ab", "label_end=4 should be at most message_byte_count=3"),
            (b"\x40", "label_prefix=0x40 should be zero"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(DNSNameRejected, expected):
                read_dns_name(message, 0, max_pointer_depth=16)

        with self.assertRaises(DNSNameRejected) as rejected:
            read_dns_name(b"\x01\xff\x00", 0, max_pointer_depth=16)

        self.assertIsInstance(rejected.exception.__cause__, UnicodeDecodeError)


class DNSResponseTests(unittest.TestCase):
    def test_aliases_and_addresses_are_read_across_response_sections(self):
        response = (
            struct.pack("!HHHHHH", 111, 0x8180, 1, 1, 1, 1) +
            b"\x07example\x07invalid\x00\x00\x01\x00\x01" +
            b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, 8) + b"\x05alias\xc0\x14" +
            b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, 8) + b"\x05other\xc0\x14" +
            b"\x05other\xc0\x14" + struct.pack("!HHIH", 1, 1, 60, 4) + b"\xcb\x00\x71\x01"
        )

        self.assertEqual(
            read_ipv4_response(response, hostname="example.invalid", transaction=111, max_pointer_depth=16),
            ({"other.invalid": ["203.0.113.1"]}, {"example.invalid": "other.invalid"}),
        )

    def test_received_a_records_are_grouped_by_owner(self):
        for (records, expected) in (
            ((), {}),
            (
                (
                    (b"\x07example\x07invalid\x00", b"\xcb\x00\x71\x01"),
                    (b"\x05other\x07invalid\x00", b"\xcb\x00\x71\x02"),
                    (b"\x07example\x07invalid\x00", b"\xcb\x00\x71\x03"),
                ),
                {"example.invalid": ["203.0.113.1", "203.0.113.3"], "other.invalid": ["203.0.113.2"]},
            ),
        ):
            with self.subTest(records=records):
                response = (
                    struct.pack("!HHHHHH", 111, 0x8180, 1, len(records), 0, 0) +
                    b"\x07example\x07invalid\x00\x00\x01\x00\x01"
                )

                for (owner, address) in records:
                    response += owner + struct.pack("!HHIH", 1, 1, 60, 4) + address

                self.assertEqual(
                    read_ipv4_response(response, hostname="example.invalid", transaction=111, max_pointer_depth=16),
                    (expected, {}),
                )

    def test_question_and_header_rejections_identify_the_failed_relationship(self):
        question = b"\x07example\x07invalid\x00\x00\x01\x00\x01"

        for (response, expected) in (
            (b"", "response_byte_count=0 should be at least min_header_bytes=12"),
            (struct.pack("!HHHHHH", 112, 0x8180, 1, 0, 0, 0), "response_id=112 should match transaction=111"),
            (struct.pack("!HHHHHH", 111, 0x0100, 1, 0, 0, 0), "flags=0x0100 should mark a response"),
            (struct.pack("!HHHHHH", 111, 0x8380, 1, 0, 0, 0), "flags=0x8380 should not mark a truncated response"),
            (struct.pack("!HHHHHH", 111, 0x8183, 1, 0, 0, 0), "rcode=3 should be zero"),
            (struct.pack("!HHHHHH", 111, 0x8180, 0, 0, 0, 0), "question_count=0 should be one"),
            (
                struct.pack("!HHHHHH", 111, 0x8180, 1, 0, 0, 0) + question,
                "question_name='example.invalid' should match expected_name='other.invalid'",
            ),
        ):
            with self.subTest(response=response), self.assertRaisesRegex(DNSResponseRejected, expected):
                read_ipv4_response(response, hostname="other.invalid", transaction=111, max_pointer_depth=16)
