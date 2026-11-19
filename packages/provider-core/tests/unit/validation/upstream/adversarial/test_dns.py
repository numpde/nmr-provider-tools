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

        with self.assertRaises(ValueError):
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

        for (encoded, offset, depth) in ((message, len(encoded_name), 16), (encoded_name, 0, 17)):
            with self.subTest(offset=offset, depth=depth), self.assertRaises(DNSNameRejected):
                read_dns_name(encoded, offset, depth, max_pointer_depth=16)

        with self.assertRaises(DNSNameRejected):
            read_dns_name(b"\xc0\x00", 0, max_pointer_depth=16)

    def test_incomplete_and_unsupported_name_encodings_are_rejected(self):
        for message in (b"", b"\xc0", b"\x03ab", b"\x40" + b"a" * 64 + b"\0"):
            with self.subTest(message=message), self.assertRaises(DNSNameRejected):
                read_dns_name(message, 0, max_pointer_depth=16)

    def test_non_ascii_wire_name_preserves_the_decode_cause(self):
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

    def test_inapplicable_replies_cannot_supply_an_ipv4_address(self):
        name = b"\x07example\x07invalid\x00"
        question = name + struct.pack("!HH", 1, 1)
        answer = name + struct.pack("!HHIH", 1, 1, 60, 4) + b"\xcb\x00\x71\x01"
        matching = struct.pack("!HHHHHH", 111, 0x8180, 1, 1, 0, 0) + question + answer

        self.assertEqual(
            read_ipv4_response(matching, hostname="example.invalid", transaction=111, max_pointer_depth=16),
            ({"example.invalid": ["203.0.113.1"]}, {}),
        )

        for (reason, response_id, flags, question_count, response_question) in (
            ("different transaction", 112, 0x8180, 1, question),
            ("query", 111, 0x0180, 1, question),
            ("truncated reply", 111, 0x8380, 1, question),
            ("name error", 111, 0x8183, 1, question),
            ("no declared question", 111, 0x8180, 0, question),
            ("different name", 111, 0x8180, 1, b"\x07another\x07invalid\x00" + struct.pack("!HH", 1, 1)),
            ("different record type", 111, 0x8180, 1, name + struct.pack("!HH", 28, 1)),
        ):
            with self.subTest(reason=reason), self.assertRaises(DNSResponseRejected):
                response = struct.pack("!HHHHHH", response_id, flags, question_count, 1, 0, 0) + response_question + answer

                read_ipv4_response(response, hostname="example.invalid", transaction=111, max_pointer_depth=16)
