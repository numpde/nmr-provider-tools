"""Job-page admission rejects envelope drift and row identity substitutions."""

import json
import os
import unittest

from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.job_feed import read_jobs_page
from tests.fakes.job_feed import page


class JobFeedValidationTests(unittest.TestCase):
    def _invalid_envelopes(self):
        for (field, bad) in (
            ("schema_id", "future.v2"),
            ("analysis_kind_ref", "other_kind"),
            ("has_provider_execution_attempt", True),
            ("has_provider_execution_attempt", 0),
            ("next_cursor", "YR"),
            ("next_cursor", "YQ="),
            ("next_cursor", ""),
            ("next_cursor", []),
            ("jobs", {}),
            ("unexpected", "private canary")
        ):
            with self.subTest(field=field):
                document = page()
                document[field] = bad

                yield field, json.dumps(document).encode()

    def test_envelope_kind_view_cursor_and_schema_drift_are_rejected(self):
        for (field, response_bytes) in self._invalid_envelopes():
            with self.subTest(field=field), self.assertRaises(ResponseRejected):
                read_jobs_page(
                    response_bytes,
                    analysis_kind_ref="nmr_predict",
                    has_provider_execution_attempt=False,
                    limit=1,
                )

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_envelope_rejection_diagnostics_omit_response_content(self):
        for (field, response_bytes) in self._invalid_envelopes():
            with self.subTest(field=field), self.assertRaises(ResponseRejected) as rejected:
                read_jobs_page(
                    response_bytes,
                    analysis_kind_ref="nmr_predict",
                    has_provider_execution_attempt=False,
                    limit=1,
                )

            self.assertNotIn("private canary", str(rejected.exception))

    def test_item_identity_calendar_and_requested_count_are_admitted(self):
        for (field, bad) in (
            ("job_ref", "foreign"),
            ("analysis_kind_ref", "other_kind"),
            ("input_schema_id", "other.v1"),
            ("input_fingerprint", "sha256:no"),
            ("input_byte_length", True),
            ("input_byte_length", 0),
            ("input_byte_length", 65537),
            ("created_at", "2026-02-30T00:00:00Z"),
            ("created_at", "2026-10-18T00:00:00.000000Z"),
            ("created_at", "2026-10-18T00:00:00+00:00"),
            ("unknown", "private canary")
        ):
            with self.subTest(field=field):
                document = page()
                document["jobs"][0][field] = bad

                with self.assertRaises(ResponseRejected):
                    read_jobs_page(
                        json.dumps(document).encode(),
                        analysis_kind_ref="nmr_predict",
                        has_provider_execution_attempt=False,
                        limit=1
                    )

        document = page()
        document["jobs"] *= 2

        with self.assertRaises(ResponseRejected):
            read_jobs_page(
                json.dumps(document).encode(),
                analysis_kind_ref="nmr_predict",
                has_provider_execution_attempt=False,
                limit=1
            )
