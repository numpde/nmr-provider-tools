"""Progress reports current observations without implying execution or liveness."""

import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, progress_response


class AttemptProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_snapshot_uses_exact_route_and_preserves_original_time(self):
        for phase in ("preparing", "running"):
            for condition in (None, "source_unavailable", "x" * 128):
                response = reply(progress_response(phase=phase, condition_code=condition))
                transport = Transport(response, response)
                api = ProviderAPI(
                    SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                    transport,
                    expected_topology="dev-local",
                )
                for _ in range(2):
                    result = await api.progress_attempt(ATTEMPT_REF, phase=phase, condition_code=condition)
                    self.assertEqual(
                        (result.execution_attempt_ref, result.phase, result.condition_code), (ATTEMPT_REF, phase, condition)
                    )
                    self.assertEqual(result.updated_at.isoformat(), "2026-10-18T00:00:00.123456+00:00")
                request, limit = transport.calls[0]
                self.assertEqual(
                    (request.method, request.url.raw_path, limit),
                    ("PUT", b"/provider/v1/execution-attempts/" + ATTEMPT_REF.encode() + b"/progress", 65536),
                )
                self.assertEqual(
                    json.loads(request.content),
                    {
                        "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
                        "phase": phase,
                        "condition_code": condition,
                    },
                )
                self.assertEqual(request.headers["Content-Type"], "application/json")
                self.assertIn("Content-Digest", request.headers)
                self.assertNotEqual(request.headers["Signature-Input"], transport.calls[1][0].headers["Signature-Input"])
