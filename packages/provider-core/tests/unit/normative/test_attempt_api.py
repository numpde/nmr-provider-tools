"""Readback preserves independent Attempt and Job lifecycle observations."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, observation


class AttemptReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_attempt_and_job_states_remain_independent(self):
        for state in ("in_progress", "succeeded", "failed", "expired"):
            for job_state in ("open", "closed", "cancelled"):
                with self.subTest(state=state, job_state=job_state):
                    transport = Transport(reply(observation(state=state, job_state=job_state)))
                    api = ProviderAPI(
                        SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                        transport,
                        expected_topology="dev-local",
                    )
                    result = await api.read_attempt(ATTEMPT_REF, job_ref="job:example")
                    self.assertEqual(
                        (result.execution_attempt_ref, result.job_ref, result.state, result.job_state),
                        (ATTEMPT_REF, "job:example", state, job_state),
                    )
                    request, limit = transport.calls[0]
                    self.assertEqual(
                        (request.method, request.url.raw_path, limit),
                        ("GET", b"/provider/v1/execution-attempts/" + ATTEMPT_REF.encode(), 65536),
                    )
                    self.assertEqual(request.content, b"")
                    self.assertIn("Signature", request.headers)
                    self.assertNotIn("Content-Type", request.headers)
                    self.assertNotIn("Content-Digest", request.headers)
