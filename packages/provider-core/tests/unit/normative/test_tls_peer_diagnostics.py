"""Explicit ownership of optional asyncio diagnostics caused by negative TLS tests."""

import logging
import unittest

from tests.fakes import tls_peer


class RejectedTlsDiagnosticTests(unittest.TestCase):
    def record(self, *, message="Error on transport creation for incoming connection",
               cause=None, path="/usr/local/lib/python3.12/asyncio/base_events.py"):
        if cause is None:
            cause = ConnectionResetError()
        return logging.LogRecord(
            "asyncio", logging.ERROR, path, 1833, message, (),
            (type(cause), cause, None),
        )

    def test_absent_server_reset_is_allowed_and_filter_does_not_leak(self):
        logger = logging.getLogger("asyncio")
        before = tuple(logger.filters)
        with tls_peer.expect_optional_certificate_rejection_reset() as records:
            self.assertEqual([], records)
            self.assertEqual(len(before) + 1, len(logger.filters))
        self.assertEqual([], records)
        self.assertEqual(before, tuple(logger.filters))

    def test_one_exact_server_reset_is_claimed_but_a_second_is_unowned(self):
        logger = logging.getLogger("asyncio")
        before = tuple(logger.filters)
        first, repeated = self.record(), self.record()
        with tls_peer.expect_optional_certificate_rejection_reset() as records:
            self.assertFalse(logger.filter(first))
            self.assertEqual([first], records)
            self.assertTrue(logger.filter(repeated))
            self.assertEqual([first], records)
        self.assertEqual(before, tuple(logger.filters))

    def test_other_asyncio_errors_are_never_claimed(self):
        logger = logging.getLogger("asyncio")
        with tls_peer.expect_optional_certificate_rejection_reset() as records:
            for record in (
                self.record(message="Task exception was never retrieved"),
                self.record(cause=ConnectionAbortedError("unexpected TLS timeout")),
                self.record(path="/package/tests/fakes/tls_peer.py"),
                self.record(path="/usr/local/lib/python3.12/other/base_events.py"),
                self.record(message="Error on transport creation for incoming connection",
                            cause=RuntimeError("unrelated failure")),
            ):
                with self.subTest(record=record):
                    self.assertTrue(logger.filter(record))
            self.assertEqual([], records)

    def test_filter_is_restored_if_the_negative_test_itself_fails(self):
        logger = logging.getLogger("asyncio")
        before = tuple(logger.filters)
        with self.assertRaisesRegex(AssertionError, "certificate rejection not proven"):
            with tls_peer.expect_optional_certificate_rejection_reset():
                raise AssertionError("certificate rejection not proven")
        self.assertEqual(before, tuple(logger.filters))
