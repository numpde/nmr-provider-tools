"""Authenticated API reads preserve exact Job bytes and issue one request."""

import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.job_input import facts, response


class ApiReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_read_binds_exact_bytes_and_signs_bodyless_route(self):
        transport = Transport(reply(response()))
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        with patch("provider_core.api.time.time", return_value=1700000000):
            verified = await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertEqual(verified.specification_bytes, b"private instructions")
        self.assertEqual(len(transport.calls), 1)
        request, ceiling = transport.calls[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.raw_path, b"/provider/v1/jobs/job:example/input?analysis_kind_ref=nmr_prediction")
        self.assertEqual(ceiling, 131072)
        self.assertNotIn("Content-Type", request.headers)
        self.assertNotIn("Content-Digest", request.headers)
        self.assertIn("Signature", request.headers)
