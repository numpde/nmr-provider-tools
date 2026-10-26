"""Inventory rejects drift before a recovery consumer receives any observations."""

import json
import traceback
import unittest

from provider_core.api import ApiFailure
from provider_core.attempt_inventory import read_attempt_inventory
from provider_core.response_json import ResponseRejected
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_inventory import page, response
from tests.fakes.api import api


class AttemptInventoryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_query_dispatches_nothing(self):
        for field, bad in (("state", "succeeded"), ("state", True), ("limit", True), ("limit", 0), ("limit", 101),
                           ("cursor", "YQ="), ("cursor", "YR"), ("cursor", ""), ("cursor", "a\n")):
            with self.subTest(field=field, bad=bad):
                arguments = {"state": "in_progress", "limit": 1, "cursor": None}
                arguments[field] = bad
                transport = Transport()
                with self.assertRaises(ValueError):
                    await api(transport, "https://api.example").execution_attempts(**arguments)
                self.assertEqual(transport.calls, [])

    async def test_bad_wire_retains_private_evidence_and_no_mutation_uncertainty(self):
        raw = response()
        malformed = (raw + b" " * 65536, b"\xff", b"{", raw.replace(b'"next_cursor": "YQ"', b'"next_cursor": "YQ", "next_cursor": null'),
                     raw.replace(b'"in_progress"', b"NaN"), response(schema_id="private canary schema"))
        for body in malformed:
            with self.subTest(size=len(body)):
                transport = Transport(reply(body))
                with self.assertRaises(ApiFailure) as failure:
                    await api(transport, "https://api.example").execution_attempts(state="in_progress", limit=1, cursor=None)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(failure.exception.response.body, body)
                self.assertFalse(failure.exception.attempt_mutation_may_have_committed)
                for rendered in (str(failure.exception), repr(failure.exception), "".join(traceback.format_exception(failure.exception))):
                    self.assertNotIn("private canary", rendered)

    def test_envelope_schema_cursor_fields_and_requested_count_are_admitted(self):
        for field, bad in (("schema_id", "future.v2"), ("attempts", {}), ("next_cursor", "YR"),
                           ("next_cursor", "YQ="), ("next_cursor", ""), ("extra", "private canary")):
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

    def test_every_row_is_admitted_before_a_page_is_exposed(self):
        for field, bad in (("analysis_kind_ref", "wrong kind"), ("execution_attempt_ref", "foreign"),
                           ("job_ref", "foreign"), ("provider_attempt_key", "private canary\n"),
                           ("provider_attempt_key", ""), ("provider_attempt_key", "a" * 129),
                           ("state", "succeeded"), ("state", {}),
                           ("started_at", "2026-02-30T00:00:00Z"), ("started_at", "2026-10-18T00:00:00.000000Z"),
                           ("started_at", "2026-10-18T00:00:00+00:00"), ("extra", "private canary")):
            document = page()
            document["attempts"].append({**document["attempts"][0], field: bad})
            with self.subTest(field=field, bad=bad), self.assertRaises(ResponseRejected) as rejected:
                read_attempt_inventory(json.dumps(document).encode(), limit=2)
            self.assertNotIn("private canary", str(rejected.exception))
