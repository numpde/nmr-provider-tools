"""Retry-After grammar supplies timing advice independently of retry permission."""

import unittest

from provider_core.validation.upstream.http import read_retry_after


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_accepts_http_time_forms(self):
        for (value, expected) in (
            (b"120", 120),
            (b"0", 0),
            (b"Sun, 06 Nov 1994 08:49:37 GMT", 0),
            (b"Sun, 06 Nov 1994 08:51:37 GMT", 120),
            (b"Sunday, 06-Nov-94 08:51:37 GMT", 120),
            (b"Sun Nov  6 08:51:37 1994", 120),
            (b"-3", None),
            (b"0.1", None),
            (b"not a date", None),
            (b"9" * 400, None),
            (b"\xff", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    read_retry_after(((b"retry-after", value),), wall_clock=lambda: 784111777),
                    expected,
                )

        self.assertIsNone(
            read_retry_after(
                ((b"retry-after", b"120"), (b"retry-after", b"240")),
                wall_clock=lambda: 784111777,
            )
        )
