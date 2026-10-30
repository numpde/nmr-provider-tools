"""Retained publication inputs reconstruct the same API signing and trust owner."""

import ssl
import unittest
from unittest.mock import Mock, patch

from provider_core.api_config import ApiConfigurationRejected, api_settings, api_settings_from_material, api_settings_with_material
from tests.fakes.credential import document, encoded


class PublicationMaterialTests(unittest.TestCase):
    def setUp(self):
        self.credential = encoded(document())
        self.fields = {
            "origin": "https://api.example",
            "profile": "dev-local",
            "topology": "dev-local",
            "provider_ref": "provider:example",
            "exchange_seconds": 10,
            "retirement_seconds": 5,
        }

    def test_retained_credential_reconstructs_the_file_based_api_owner(self):
        reader = Mock(return_value=self.credential)
        file_settings, file_provider = api_settings(self.fields | {"credential_file": "/private/credential"}, reader)
        retained, provider = api_settings_from_material(self.fields, credential_bytes=self.credential)
        self.assertEqual(provider, file_provider)
        self.assertEqual(retained.identity.origin, file_settings.identity.origin)
        self.assertEqual(retained.identity.credential_ref, file_settings.identity.credential_ref)
        self.assertEqual(retained.expected_topology, file_settings.expected_topology)
        self.assertEqual(retained.http.exchange_seconds, file_settings.http.exchange_seconds)
        self.assertEqual(retained.http.retirement_seconds, file_settings.http.retirement_seconds)
        self.assertEqual(retained.http.trust.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(retained.http.trust.check_hostname)
        reader.assert_called_once_with("/private/credential", maximum_bytes=16384,
                                       label="API signing credential", secret=True)

    def test_provider_captures_the_same_admitted_material_once(self):
        reader = Mock(return_value=self.credential)
        original, provider, material = api_settings_with_material(
            self.fields | {"credential_file": "/private/credential"}, reader
        )
        self.assertEqual(material.credential_bytes, self.credential)
        self.assertIsNone(material.ca_bytes)
        self.assertNotIn("PRIVATE KEY", repr(material))
        rebuilt, rebuilt_provider = api_settings_from_material(
            material.api_fields(), credential_bytes=material.credential_bytes, ca_bytes=material.ca_bytes
        )
        self.assertEqual(provider, rebuilt_provider)
        self.assertEqual(original.identity.credential_ref, rebuilt.identity.credential_ref)
        self.assertEqual(original.identity.origin, rebuilt.identity.origin)
        reader.assert_called_once()

    def test_explicit_bad_ca_never_falls_back_to_system_roots(self):
        with patch("provider_core.api_config.ssl.create_default_context") as roots:
            with self.assertRaises(ApiConfigurationRejected):
                api_settings_from_material(self.fields, credential_bytes=self.credential, ca_bytes=b"")
        roots.assert_not_called()

    def test_wrong_identity_is_refused_before_ca_loading(self):
        reader = Mock(return_value=self.credential)
        fields = self.fields | {"provider_ref": "provider:other", "credential_file": "/private/credential",
                                "ca_file": "/private/ca"}
        with self.assertRaises(ApiConfigurationRejected):
            api_settings(fields, reader)
        reader.assert_called_once()
        with self.assertRaises(ApiConfigurationRejected):
            api_settings_from_material(self.fields | {"provider_ref": "provider:other"},
                                       credential_bytes=self.credential, ca_bytes=b"invalid CA")
