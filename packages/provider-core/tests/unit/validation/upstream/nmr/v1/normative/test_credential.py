"""Artifact admission preserves the issued identity and matching signing key."""

import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.validation.upstream.nmr.v1.credential import parse_provider_credential
from tests.fakes.credential import CREDENTIAL_PARSER_BYTES, document, encoded


class ProviderCredentialTests(unittest.TestCase):
    def _admitted_credentials(self):
        key = Ed25519PrivateKey.generate()

        for profile in ("dev-local", "dev", "run"):
            with self.subTest(profile=profile):
                original = document(key=key, profile=profile)
                credential = parse_provider_credential(encoded(original), maximum_bytes=CREDENTIAL_PARSER_BYTES)

                yield (credential, original, key, profile)

    def test_original_artifact_preserves_identity_and_signing_capability(self):
        for (credential, _original, key, profile) in self._admitted_credentials():
            self.assertEqual(
                (credential.profile, credential.provider_ref, credential.credential_ref),
                (profile, "provider:example", "credential:example"),
            )

            key.public_key().verify(credential.private_key.sign(b"admitted capability"), b"admitted capability")

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_credential_representation_omits_signing_material(self):
        for (credential, original, _key, _profile) in self._admitted_credentials():
            self.assertNotIn(original["private_key_pkcs8_pem"], repr(credential))
            self.assertNotIn("private_key=", repr(credential))
