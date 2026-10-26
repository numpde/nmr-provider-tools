"""The configured deployment marker admits replies without rewriting Job bytes."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.job_input import facts, response


class ApiEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_explicit_environment_and_case_insensitive_marker(self):
        for topology in ("dev-local", "dev", "web"):
            with self.subTest(topology=topology):
                transport = Transport(
                    reply(
                        response(),
                        headers=(
                            (b"Content-Type", b"application/json"),
                            (b"Cache-Control", b"no-store"),
                            (b"Nmr-Api-Topology", topology.encode()),
                        ),
                    )
                )
                api = ProviderAPI(
                    SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                    transport,
                    expected_topology=topology,
                )
                observed = await api.job_input(facts(), analysis_kind_ref="nmr_predict")
                self.assertEqual(observed.specification_bytes, b"private instructions")
                self.assertEqual(api.expected_topology, topology)
