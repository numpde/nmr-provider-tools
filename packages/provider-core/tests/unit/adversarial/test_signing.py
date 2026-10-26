"""Reject ambiguous signing targets before any HTTP operation is possible."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.signing import SigningIdentity, signed_request


class SigningRejectionTests(unittest.TestCase):
    def test_origin_and_credential_cannot_inject_or_redirect(self):
        key = Ed25519PrivateKey.generate()
        for origin in (
            "http://api.example",
            "https://user:secret@api.example",
            "https://api.example/path",
            "https://api.example?query",
            "https://api.example#fragment",
            "https://api.example\n",
            "https://api.example:0",
            "https://api.example:",
            "https://[v1.example]",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                SigningIdentity(origin, "credential:test", key)
        for credential in ("wrong", "credential:", "credential:x\n", "credential:" + "x" * 128):
            with self.subTest(credential=credential), self.assertRaises(ValueError):
                SigningIdentity("https://api.example", credential, key)

    def test_rewritten_or_ambiguous_targets_and_invalid_signature_inputs_fail(self):
        identity = SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate())
        defaults = {"method": "GET", "path": "/input", "query": b"", "body": None, "created": 1, "nonce": bytes(16)}
        for changes in (
            {"method": "get"},
            {"method": "GET\r\nInjected: yes"},
            {"path": "//other.example/input"},
            {"path": "/a/../input"},
            {"path": "/input?x"},
            {"path": "/input#x"},
            {"path": "/input\n"},
            {"query": b"a=space here"},
            {"query": b"a=#fragment"},
            {"body": bytearray(b"mutable")},
            {"created": True},
            {"created": -1},
            {"nonce": b"short"},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                signed_request(identity, **(defaults | changes))

    def test_identity_repr_does_not_expose_signing_material(self):
        identity = SigningIdentity("https://api.example", "credential:private-canary", Ed25519PrivateKey.generate())
        self.assertNotIn("private-canary", repr(identity))
        self.assertNotIn("private_key", repr(identity))
