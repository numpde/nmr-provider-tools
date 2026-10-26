"""Discovery refuses protocol drift and substitutions before input/science work."""

import json
import traceback
import unittest

from provider_core.api import ApiFailure
from provider_core.job_feed import read_jobs_page
from provider_core.response_json import ResponseRejected
from tests.fakes.api import Transport, reply
from tests.fakes.job_feed import page, response
from tests.fakes.api import api


class JobFeedFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_oversized_or_malformed_wire_is_never_returned_as_a_page(self):
        raw = response()
        for malformed in (raw + b" " * 65536, raw.replace(b'"next_cursor": "YQ"', b'"next_cursor": "YQ", "next_cursor": null'),
                          b"\xff", b"{", raw.replace(b'"input_byte_length": 20', b'"input_byte_length": NaN')):
            with self.subTest(size=len(malformed)):
                transport = Transport(reply(malformed))
                with self.assertRaises(ApiFailure):
                    await api(transport, "https://api.example").jobs(analysis_kind_ref="nmr_predict",
                                                                 has_provider_execution_attempt=False, limit=1, cursor=None)
                self.assertEqual(len(transport.calls), 1)

    async def test_bad_query_dispatches_nothing(self):
        for field, bad in (("analysis_kind_ref", "wrong kind"), ("limit", True), ("limit", 0), ("limit", 101),
                           ("has_provider_execution_attempt", 0), ("cursor", "YQ="), ("cursor", "YR"), ("cursor", "")):
            with self.subTest(field=field, bad=bad):
                arguments = {"analysis_kind_ref": "nmr_predict", "has_provider_execution_attempt": False, "limit": 1, "cursor": None}
                arguments[field] = bad
                transport = Transport()
                with self.assertRaises(ValueError):
                    await api(transport, "https://api.example").jobs(**arguments)
                self.assertEqual(transport.calls, [])

    def test_envelope_kind_view_cursor_and_schema_drift_are_rejected(self):
        for field, bad in (("schema_id", "future.v2"), ("analysis_kind_ref", "other_kind"),
                           ("has_provider_execution_attempt", True), ("has_provider_execution_attempt", 0),
                           ("next_cursor", "YR"), ("next_cursor", "YQ="), ("next_cursor", ""),
                           ("jobs", {}), ("unexpected", "private canary")):
            with self.subTest(field=field):
                document = page()
                document[field] = bad
                with self.assertRaises(ResponseRejected) as rejected:
                    read_jobs_page(json.dumps(document).encode(), analysis_kind_ref="nmr_predict",
                                   has_provider_execution_attempt=False, limit=1)
                self.assertNotIn("private canary", str(rejected.exception))

    def test_item_identity_calendar_and_requested_count_are_admitted(self):
        for field, bad in (("job_ref", "foreign"), ("analysis_kind_ref", "other_kind"),
                           ("input_schema_id", "other.v1"), ("input_fingerprint", "sha256:no"),
                           ("input_byte_length", True), ("input_byte_length", 0), ("input_byte_length", 65537),
                           ("created_at", "2026-02-30T00:00:00Z"), ("created_at", "2026-10-18T00:00:00.000000Z"),
                           ("created_at", "2026-10-18T00:00:00+00:00"), ("unknown", "private canary")):
            with self.subTest(field=field):
                document = page()
                document["jobs"][0][field] = bad
                with self.assertRaises(ResponseRejected):
                    read_jobs_page(json.dumps(document).encode(), analysis_kind_ref="nmr_predict",
                                   has_provider_execution_attempt=False, limit=1)
        document = page()
        document["jobs"] *= 2
        with self.assertRaises(ResponseRejected):
            read_jobs_page(json.dumps(document).encode(), analysis_kind_ref="nmr_predict",
                           has_provider_execution_attempt=False, limit=1)

    async def test_wire_failure_retains_private_response_and_read_effect_truth(self):
        raw = response().replace(b"nmr.provider.jobs.list.response.v1", b"private canary schema")
        transport = Transport(reply(raw))
        with self.assertRaises(ApiFailure) as failure:
            await api(transport, "https://api.example").jobs(analysis_kind_ref="nmr_predict",
                                                         has_provider_execution_attempt=False, limit=1, cursor=None)
        self.assertEqual(failure.exception.response.body, raw)
        self.assertFalse(failure.exception.attempt_mutation_may_have_committed)
        self.assertNotIn("private canary", str(failure.exception))
        self.assertNotIn("private canary", repr(failure.exception))
        self.assertNotIn("private canary", "".join(traceback.format_exception(failure.exception)))
