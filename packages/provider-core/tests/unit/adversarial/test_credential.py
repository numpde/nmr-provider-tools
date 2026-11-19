"""Substituted identities, ambiguous encodings and mismatched keys never sign."""

import base64
import json
import unittest
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from provider_core.credential import CredentialRejected, parse_provider_credential
from tests.fakes.credential import document, encoded


class ProviderCredentialBoundaryTests(unittest.TestCase):
    def _untrusted_identity_artifacts(self):
        baseline = document()

        cases = (
            {"schema_id": "nmr.user.private_signing_credential.v1"},
            {"profile": "PRIVATE_PROFILE"},
            {"principal_ref": "user:example"},
            {"credential_ref": "PRIVATE_KEY invalid reference"},
            {"algorithm": "rsa"},
            {"principal_ref": True},
            {"private_key_pkcs8_pem": []},
            {"PRIVATE_FIELD": "PRIVATE_VALUE"},
        )

        for change in cases:
            yield next(iter(change)), encoded(baseline | change), baseline["private_key_pkcs8_pem"]

    def test_closed_provider_artifact_rejects_untrusted_identity_fields(self):
        for (field, artifact_bytes, _private_key) in self._untrusted_identity_artifacts():
            with self.subTest(field=field), self.assertRaises(CredentialRejected):
                parse_provider_credential(artifact_bytes)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_identity_rejection_diagnostics_omit_fields_and_signing_material(self):
        for (field, artifact_bytes, private_key) in self._untrusted_identity_artifacts():
            with self.subTest(field=field), self.assertRaises(CredentialRejected) as caught:
                parse_provider_credential(artifact_bytes)

            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertNotIn(private_key, str(caught.exception))

    def test_public_and_private_keys_must_belong_together(self):
        (first, second) = (document(), document())

        with self.assertRaisesRegex(CredentialRejected, "do not match"):
            parse_provider_credential(encoded(first | {"public_key_spki_der_b64": second["public_key_spki_der_b64"]}))

    def test_matching_keys_with_another_algorithm_cannot_claim_ed25519(self):
        artifact = document(key=X25519PrivateKey.generate())

        with self.assertRaisesRegex(expected_exception=CredentialRejected, expected_regex=r"type\(public_key\)=.*X25519PublicKey.* ≠ expected_type=.*Ed25519PublicKey"):
            parse_provider_credential(encoded(artifact))

    def test_public_key_padding_bits_must_be_canonical(self):
        artifact = document()
        public = artifact["public_key_spki_der_b64"]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        alternate = public[:-2] + alphabet[alphabet.index(public[-2]) + 1] + "="

        self.assertEqual(base64.b64decode(alternate), base64.b64decode(public))

        with self.assertRaises(CredentialRejected):
            parse_provider_credential(encoded(artifact | {"public_key_spki_der_b64": alternate}))

    def test_original_canonical_bytes_are_required_without_json_fallback(self):
        baseline = document()
        raw = encoded(baseline)

        for candidate in (
            raw[:-1],
            raw + b"\n",
            b" " + raw,
            b"\xff",
            b"{}\n",
            b"[]\n",
            raw.replace(b'"algorithm":"ed25519"', b'"algorithm":"ed25519","algorithm":"ed25519"'),
            json.dumps(baseline, indent=2).encode() + b"\n",
            b"x" * 16385,
        ):
            with self.subTest(length=len(candidate)), self.assertRaises(CredentialRejected):
                parse_provider_credential(candidate)

    def _invalid_public_key_artifacts(self):
        baseline = document()
        raw = base64.b64decode(baseline["public_key_spki_der_b64"])

        for public in (
            "PRIVATE invalid base64",
            base64.b64encode(b"not DER").decode(),
            base64.b64encode(raw + b"trailing").decode(),
        ):
            yield len(public), encoded(baseline | {"public_key_spki_der_b64": public})

    def test_invalid_or_noncanonical_public_key_encodings_are_rejected(self):
        for (public_length, artifact_bytes) in self._invalid_public_key_artifacts():
            with self.subTest(public_length=public_length), self.assertRaises(CredentialRejected):
                parse_provider_credential(artifact_bytes)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_public_key_rejection_diagnostics_omit_input(self):
        for (public_length, artifact_bytes) in self._invalid_public_key_artifacts():
            with self.subTest(public_length=public_length), self.assertRaises(CredentialRejected) as caught:
                parse_provider_credential(artifact_bytes)

            self.assertNotIn("PRIVATE", str(caught.exception))

    def _private_key_failure(self):
        baseline = document(private_key_pkcs8_pem="PRIVATE malformed key")

        with self.assertRaises(CredentialRejected) as caught:
            parse_provider_credential(encoded(baseline))

        return caught.exception

    def test_private_key_decoder_failure_has_operation_context(self):
        failure = self._private_key_failure()

        self.assertIn("private key", str(failure))
        self.assertIsNotNone(failure.cause)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_private_key_decoder_diagnostics_omit_input(self):
        self.assertNotIn("PRIVATE", str(self._private_key_failure()))
