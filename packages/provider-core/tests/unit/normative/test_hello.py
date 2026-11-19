"""A Hello describes the caller's complete current offering, including an empty set."""

import json
import unittest
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering
from provider_core.adapters.nmr.v1.signing import SigningIdentity
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
            (result.provider_ref, result.accepted_at),
            ("provider:example", datetime(2026, 10, 20, 10, 0, 0, 123456, UTC))
        )

        await api.publish_hello(**announcement(display_name="Changed", analysis_offerings=()))
        ((one, maximum), (two, _)) = transport.calls

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
