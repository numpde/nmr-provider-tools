"""Missing local listeners retry without admitting permanent or unowned failures."""

import asyncio
import errno
import ssl
import unittest
from pathlib import Path

import httpcore

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryUnavailable
from provider_core.transport import ExchangeFailed


class DeliveryAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = DeliveryClient(
            DeliveryEndpoint("https://api.example", "provider:example", Path("/tmp/delivery-test.sock")),
            exchange_seconds=1, retirement_seconds=1,
        )

    def failure(self, primary, **facts):
        failure = DeliveryUnavailable("Local relay unavailable", cause=ExchangeFailed("Local HTTP failed", primary=primary, **facts))
        self.client.last_failure = failure
        return failure

    def test_missing_listener_in_native_cause_or_context_requires_exact_owned_settled_failure(self):
        for attribute in ("__cause__", "__context__"):
            with self.subTest(attribute=attribute):
                primary = httpcore.ConnectError("PRIVATE_WRAPPER")
                setattr(primary, attribute, FileNotFoundError(errno.ENOENT, "PRIVATE_PATH"))
                failure = self.failure(primary)
                self.assertTrue(self.client.can_retry(failure))
                self.client.last_failure = None
                self.assertFalse(self.client.can_retry(failure))
                self.client.last_failure = failure
                failure.cause.cleanup_failures = (OSError(errno.EIO, "PRIVATE_CLEANUP"),)
                self.assertFalse(self.client.can_retry(failure))
                failure.cause.cleanup_failures = ()
                failure.cause.interruption = asyncio.CancelledError()
                self.assertFalse(self.client.can_retry(failure))

    def test_explicit_tls_or_permission_cause_is_not_overridden_by_missing_file_context(self):
        for cause in (ssl.SSLCertVerificationError("PRIVATE_TLS"), PermissionError(errno.EACCES, "PRIVATE_PATH")):
            with self.subTest(kind=type(cause).__name__):
                primary = httpcore.ConnectError("PRIVATE_WRAPPER")
                primary.__cause__ = cause
                primary.__context__ = FileNotFoundError(errno.ENOENT, "PRIVATE_PATH")
                self.assertFalse(self.client.can_retry(self.failure(primary)))
        unrelated = RuntimeError("PRIVATE_UNREVIEWED_WRAPPER")
        unrelated.__context__ = FileNotFoundError(errno.ENOENT, "PRIVATE_PATH")
        self.assertFalse(self.client.can_retry(self.failure(unrelated)))
