"""Announcement encoding preserves Unicode scalars and permitted whitespace."""

import json
import unittest

from provider_core.adapters.nmr.v1.hello import hello_body
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering


class HelloEncodingTests(unittest.TestCase):
    def test_maximum_unicode_snapshot_stays_within_wire_cap_without_losing_scalars(self):
        # repo-check: allow-tuple-generator -- This maximum tuple specimen proves the Hello collection boundary.
        offerings = tuple(AnalysisOffering("kind_" + str(index), "🧪" * 1024) for index in range(64))

        body = hello_body(display_name="🧪" * 128, description="🧪" * 1024, analysis_offerings=offerings)

        self.assertLessEqual(len(body), 512 * 1024)
        self.assertIn("🧪".encode(), body)
        self.assertEqual([item["description"] for item in json.loads(body)["analysis_offerings"]], ["🧪" * 1024] * 64)

    def test_api_permitted_whitespace_is_preserved(self):
        body = hello_body(
            display_name=" \t",
            description="line\n"
            "next",
            analysis_offerings=(),
        )

        self.assertEqual(json.loads(body)["display_name"], " \t")

        self.assertEqual(
            json.loads(body)["description"],
            "line\n"
            "next",
        )
