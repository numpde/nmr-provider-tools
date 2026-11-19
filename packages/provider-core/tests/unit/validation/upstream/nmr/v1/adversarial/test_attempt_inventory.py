"""Inventory admission rejects incomplete envelopes and unadmitted rows."""

import json
import os
import unittest

from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.attempt_inventory import read_attempt_inventory
from tests.fakes.attempt_inventory import page


class AttemptInventoryValidationTests(unittest.TestCase):
    def test_envelope_schema_cursor_fields_and_requested_count_are_admitted(self):
        for (field, bad) in (
            ("schema_id", "future.v2"),
            ("attempts", {}),
            ("next_cursor", "YR"),
            ("next_cursor", "YQ="),
            ("next_cursor", ""),
            ("next_cursor", []),
            ("extra", "private canary")
        ):
            document = page(**{field: bad})

            with self.subTest(field=field), self.assertRaises(ResponseRejected):
                read_attempt_inventory(json.dumps(document).encode(), limit=1)

        document = page()
        document["attempts"] *= 2

        with self.assertRaises(ResponseRejected):
            read_attempt_inventory(json.dumps(document).encode(), limit=1)

        del document["next_cursor"]

        with self.assertRaises(ResponseRejected):
            read_attempt_inventory(json.dumps(document).encode(), limit=2)

    def _invalid_inventory_rows(self):
        for (field, bad) in (
            ("analysis_kind_ref", "wrong kind"),
            ("execution_attempt_ref", "foreign"),
            ("job_ref", "foreign"),
            ("job_ref", []),
            ("provider_attempt_key", "private canary\n"),
            ("provider_attempt_key", ""),
            ("provider_attempt_key", "a" * 129),
            ("state", "succeeded"),
            ("state", {}),
            ("started_at", "2026-02-30T00:00:00Z"),
            ("started_at", "2026-10-18T00:00:00.000000Z"),
            ("started_at", "2026-10-18T00:00:00+00:00"),
            ("extra", "private canary")
        ):
            inventory_page = page()

            inventory_page["attempts"].append({**inventory_page["attempts"][0], field: bad})

            yield field, bad, json.dumps(inventory_page).encode()

    def test_every_row_is_admitted_before_a_page_is_exposed(self):
        for (field, bad, inventory_bytes) in self._invalid_inventory_rows():
            with self.subTest(field=field, bad=bad), self.assertRaises(ResponseRejected):
                read_attempt_inventory(inventory_bytes, limit=2)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_row_rejections_omit_response_content(self):
        for (field, bad, inventory_bytes) in self._invalid_inventory_rows():
            with self.subTest(field=field, bad=bad), self.assertRaises(ResponseRejected) as rejected:
                read_attempt_inventory(inventory_bytes, limit=2)

            self.assertNotIn("private canary", str(rejected.exception))
