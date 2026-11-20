"""Readback preserves independent Attempt and Job lifecycle observations."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.adapters.nmr.v1.signing import SigningIdentity
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

                    attempt_observation = await api.read_attempt(ATTEMPT_REF, job_ref="job:example")

                    self.assertEqual(
                        (attempt_observation.execution_attempt_ref, attempt_observation.job_ref, attempt_observation.state, attempt_observation.job_state),
                        (ATTEMPT_REF, "job:example", state, job_state),
                    )

                    (signed_request, max_response_bytes) = transport.calls[0]

                    self.assertEqual(
                        (signed_request.method, signed_request.url.raw_path, max_response_bytes),
                        ("GET", b"/provider/v1/execution-attempts/" + ATTEMPT_REF.encode(), 65536),
                    )

                    self.assertEqual(signed_request.content, b"")
                    self.assertIn("Signature", signed_request.headers)
                    self.assertNotIn("Content-Type", signed_request.headers)
                    self.assertNotIn("Content-Digest", signed_request.headers)
