"""CA parser stages retain safe reasons and protected causes; the reader owns file admission."""

import json
import ssl
import unittest
from unittest.mock import Mock, patch

from provider_core.api_config import ApiConfigurationRejected, https_settings
from provider_core.diagnostics import failure_evidence


class HttpsAdmissionTests(unittest.TestCase):
    def settings(self, read_file):
        return https_settings({"ca_file": "/PRIVATE_PATH/ca.pem", "exchange_seconds": 10, "retirement_seconds": 5},
                              "API", read_file)

    def test_encoding_and_certificate_failures_are_distinct_in_safe_rendered_evidence(self):
        for raw, reason, cause_type in ((b"PRIVATE_CA\xff", "ASCII", UnicodeDecodeError),
                                        (b"PRIVATE_CA", "OpenSSL", ssl.SSLError)):
            calls = []

            def read_file(path, *, maximum_bytes, label):
                calls.append((path, maximum_bytes, label))
                return raw

            with self.subTest(reason=reason), self.assertRaises(ApiConfigurationRejected) as caught:
                self.settings(read_file)
            error = caught.exception
            self.assertIn(reason, str(error))
            self.assertIsInstance(error.cause, cause_type)
            self.assertEqual(calls, [("/PRIVATE_PATH/ca.pem", 1048576, "API CA")])
            rendered = json.dumps(failure_evidence(error, secrets=()))
            self.assertIn(reason, rendered)
            self.assertIn(cause_type.__name__, rendered)
            self.assertNotIn("PRIVATE", rendered)

    def test_openssl_failure_retains_its_original_cause_without_rendering_native_text(self):
        cause = ssl.SSLError("PRIVATE_NATIVE_REASON")
        context = Mock()
        context.load_verify_locations.side_effect = cause
        with patch("provider_core.api_config.ssl.SSLContext", return_value=context), \
                self.assertRaises(ApiConfigurationRejected) as caught:
            self.settings(lambda path, **selection: b"PRIVATE_CA")
        self.assertIs(caught.exception.cause, cause)
        self.assertNotIn("PRIVATE", json.dumps(failure_evidence(caught.exception, secrets=())))

    def test_reader_failure_keeps_its_identity_before_any_tls_parser_effect(self):
        cause = OSError("The selected CA exceeds its 1048576-byte read limit.")
        reader = Mock(side_effect=cause)
        with patch("provider_core.api_config.ssl.SSLContext") as create_context, self.assertRaises(OSError) as caught:
            self.settings(reader)
        self.assertIs(caught.exception, cause)
        create_context.assert_not_called()
        reader.assert_called_once_with("/PRIVATE_PATH/ca.pem", maximum_bytes=1048576, label="API CA")

    def test_explicit_ca_reader_cannot_silently_select_system_roots(self):
        with patch("provider_core.api_config.ssl.create_default_context") as roots:
            with self.assertRaisesRegex(ApiConfigurationRejected, "reader did not return bytes"):
                self.settings(lambda path, **selection: None)
        roots.assert_not_called()
