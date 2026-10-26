"""A Hello describes the caller's complete current offering, including an empty set."""

import json
import unittest
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.hello import AnalysisOffering, hello_body
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.hello import announcement, receipt


class HelloTests(unittest.IsolatedAsyncioTestCase):
    async def test_caller_chosen_announcements_preserve_complete_snapshot_and_receipt(self):
        transport = Transport(reply(receipt()), reply(receipt()))
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        first = announcement(analysis_offerings=(AnalysisOffering("second_kind", "Two"), AnalysisOffering("first_kind", "One")))
        result = await api.publish_hello(**first)
        self.assertEqual(
            (result.provider_ref, result.accepted_at), ("provider:example", datetime(2026, 10, 20, 10, 0, 0, 123456, UTC))
        )
        await api.publish_hello(**announcement(display_name="Changed", analysis_offerings=()))
        (one, maximum), (two, _) = transport.calls
        self.assertEqual((one.method, one.url.raw_path, maximum), ("POST", b"/provider/v1/hello", 65536))
        self.assertEqual(
            json.loads(one.content),
            {
                "schema_id": "nmr.provider.hello_request.v1",
                "display_name": first["display_name"],
                "description": first["description"],
                "analysis_offerings": [
                    {"analysis_kind_ref": "second_kind", "description": "Two"},
                    {"analysis_kind_ref": "first_kind", "description": "One"},
                ],
            },
        )
        self.assertEqual(json.loads(two.content)["analysis_offerings"], [])
        self.assertEqual(json.loads(two.content)["display_name"], "Changed")
        self.assertNotEqual(one.headers["Signature-Input"], two.headers["Signature-Input"])
        self.assertIsNone(api.last_failure)

    def test_maximum_unicode_snapshot_stays_within_wire_cap_without_losing_scalars(self):
        # repo-check: allow-tuple-generator -- This maximum tuple specimen proves the Hello collection boundary.
        offerings = tuple(AnalysisOffering("kind_" + str(index), "🧪" * 1024) for index in range(64))
        body = hello_body(display_name="🧪" * 128, description="🧪" * 1024, analysis_offerings=offerings)
        self.assertLessEqual(len(body), 512 * 1024)
        self.assertIn("🧪".encode(), body)
        self.assertEqual([item["description"] for item in json.loads(body)["analysis_offerings"]], ["🧪" * 1024] * 64)

    def test_api_permitted_whitespace_is_preserved(self):
        body = hello_body(display_name=" \t", description="line\nnext", analysis_offerings=())
        self.assertEqual(json.loads(body)["display_name"], " \t")
        self.assertEqual(json.loads(body)["description"], "line\nnext")
