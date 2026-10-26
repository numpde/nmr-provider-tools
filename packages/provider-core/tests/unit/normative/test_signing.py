"""Signatures verify against the API profile, independently of signer rendering."""

import base64
import hashlib
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.signing import SigningIdentity, signed_request


class SigningTests(unittest.TestCase):
    def test_ipv6_origin_keeps_its_bracketed_authority(self):
        identity = SigningIdentity("https://[::1]:8443", "credential:test", Ed25519PrivateKey.generate())
        self.assertEqual(identity.origin, "https://[::1]:8443")
        self.assertEqual(identity.authority, "[::1]:8443")

    def test_exact_get_and_json_body_signatures(self):
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        identity = SigningIdentity("https://api.example", "credential:test", key)
        for body in (None, b"", b'{"original": "bytes"}'):
            with self.subTest(body=body):
                # silent-defaults: allow-contract -- None denotes GET without a body; even empty bytes select a supplied PUT body.
                method = "GET" if body is None else "PUT"
                request = signed_request(
                    identity,
                    method=method,
                    path="/provider/input",
                    query=b"job=job%3Ax&kind=a%2Fb",
                    body=body,
                    created=1700000000,
                    nonce=bytes(16),
                )
                self.assertEqual(request.url.raw_path, b"/provider/input?job=job%3Ax&kind=a%2Fb")
                # silent-defaults: allow-contract -- HTTPX exposes both as empty content; the following digest assertions preserve the supplied-body distinction.
                self.assertEqual(request.content, body or b"")
                self.assertEqual(request.headers["Host"], "api.example")
                components = '"@method" "@authority" "@path" "@query"'
                lines = [
                    f'"@method": {method}',
                    '"@authority": api.example',
                    '"@path": /provider/input',
                    '"@query": ?job=job%3Ax&kind=a%2Fb',
                ]
                if body is not None:
                    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
                    self.assertEqual(request.headers["Content-Type"], "application/json")
                    self.assertEqual(request.headers["Content-Digest"], f"sha-256=:{digest}:")
                    components += ' "content-type" "content-digest"'
                    lines += ['"content-type": application/json', f'"content-digest": sha-256=:{digest}:']
                else:
                    self.assertNotIn("Content-Type", request.headers)
                    self.assertNotIn("Content-Digest", request.headers)
                parameters = (
                    f"({components});created=1700000000;expires=1700000300;"
                    'nonce="AAAAAAAAAAAAAAAAAAAAAA";keyid="credential:test";tag="nmr-api-v1"'
                )
                self.assertEqual(request.headers["Signature-Input"], "sig1=" + parameters)
                lines.append('"@signature-params": ' + parameters)
                signature = parse(request.headers["Signature"].encode(), tltype="dictionary")["sig1"][0]
                key.public_key().verify(signature, "\n".join(lines).encode())

    def test_empty_query_still_has_question_mark_in_signature_base(self):
        key = Ed25519PrivateKey.generate()
        request = signed_request(
            SigningIdentity("https://api.example:8443", "credential:test", key),
            method="GET",
            path="/input",
            query=b"",
            body=None,
            created=1,
            nonce=bytes(16),
        )
        parameters = request.headers["Signature-Input"].removeprefix("sig1=")
        base = "\n".join(
            [
                '"@method": GET',
                '"@authority": api.example:8443',
                '"@path": /input',
                '"@query": ?',
                '"@signature-params": ' + parameters,
            ]
        )
        signature = parse(request.headers["Signature"].encode(), tltype="dictionary")["sig1"][0]
        key.public_key().verify(signature, base.encode())
