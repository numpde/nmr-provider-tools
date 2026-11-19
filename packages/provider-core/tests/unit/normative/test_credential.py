"""API-issued provider identity admits the same key used for signed operations."""

import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.credential import parse_provider_credential
from provider_core.signing import SigningIdentity, signed_request
from tests.fakes.credential import document, encoded


class ProviderCredentialTests(unittest.TestCase):
    def _admitted_credentials(self):
        key = Ed25519PrivateKey.generate()

        for profile in ("dev-local", "dev", "run"):
            with self.subTest(profile=profile):
                original = document(key=key, profile=profile)
                credential = parse_provider_credential(encoded(original))

                yield credential, original, key, profile

    def test_original_artifact_preserves_identity_and_signing_capability(self):
        for (credential, _original, key, profile) in self._admitted_credentials():
            self.assertEqual(
                (credential.profile, credential.provider_ref, credential.credential_ref),
                (profile, "provider:example", "credential:example"),
            )

            key.public_key().verify(credential.private_key.sign(b"admitted capability"), b"admitted capability")
            identity = SigningIdentity("https://api.example", credential.credential_ref, credential.private_key)

            request = signed_request(
                identity,
                method="GET",
                path="/provider/v1/jobs",
                query=b"",
                body=None,
                created=1700000000,
                nonce=bytes(16)
            )

            self.assertIn('keyid="credential:example"', request.headers["Signature-Input"])

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    def test_credential_representation_omits_signing_material(self):
        for (credential, original, _key, _profile) in self._admitted_credentials():
            self.assertNotIn(original["private_key_pkcs8_pem"], repr(credential))
            self.assertNotIn("private_key=", repr(credential))
