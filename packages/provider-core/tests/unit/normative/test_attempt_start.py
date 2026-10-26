"""Recover admission with original start bytes and fresh request authentication."""

import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.attempt_request import start_body
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import retained_start, start_response


class AttemptStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_start_bytes_survive_explicit_replay_and_terminal_reply(self):
        body = retained_start()
        for state in ("in_progress", "succeeded", "failed", "expired"):
            transport = Transport(reply(start_response()), reply(start_response(state=state, replayed=True)))
            api = ProviderAPI(
                SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            for replayed in (False, True):
                result = await api.start_attempt(
                    body, job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
                )
                self.assertEqual(result.replayed, replayed)
            self.assertEqual(result.state, state)
            self.assertEqual(result.started_at.isoformat(), "2026-10-18T00:00:00+00:00")
            first, second = (item[0] for item in transport.calls)
            self.assertEqual(first.content, body)
            self.assertEqual(second.content, body)
            self.assertEqual(first.headers["Content-Digest"], second.headers["Content-Digest"])
            self.assertNotEqual(first.headers["Signature-Input"], second.headers["Signature-Input"])
            self.assertEqual(
                (first.method, first.url.raw_path, transport.calls[0][1]),
                ("POST", b"/provider/v1/execution-attempts/start", 65536),
            )
            self.assertEqual(first.headers["Content-Type"], "application/json")

    def test_builder_preserves_explicit_key_without_inventing_identity(self):
        body = start_body("job:example", "persisted:key-1")
        self.assertIs(type(body), bytes)
        self.assertEqual(
            json.loads(body),
            {
                "schema_id": "nmr.provider.execution_attempt_start_request.v1",
                "job_ref": "job:example",
                "provider_attempt_key": "persisted:key-1",
            },
        )
