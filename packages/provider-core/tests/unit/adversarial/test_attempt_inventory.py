"""Inventory rejects drift before a recovery consumer receives any observations."""

import json
import traceback
import unittest
import os

from provider_core.api_errors import ApiFailure
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_inventory import response
from tests.fakes.api import api


class AttemptInventoryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_query_dispatches_nothing(self):
        for (field, bad, error_type) in (
            ("state", "succeeded", ValueError),
            ("state", True, ValueError),
            ("limit", True, TypeError),
            ("limit", 0, ValueError),
            ("limit", 101, ValueError),
            ("cursor", "YQ=", ValueError),
            ("cursor", "YR", ValueError),
            ("cursor", "a", ValueError),
            ("cursor", "", ValueError),
            ("cursor", "a\n", ValueError),
        ):
            with self.subTest(field=field, bad=bad):
                arguments = {"state": "in_progress", "limit": 1, "cursor": None}
                arguments[field] = bad
                transport = Transport()

                with self.assertRaises(error_type):
                    await api(transport, "https://api.example").execution_attempts(**arguments)

                self.assertEqual(transport.calls, [])

    async def _bad_wire_failures(self):
        raw = response()

        malformed = (
            raw + b" " * 65536,
            b"\xff",
            b"{",
            raw.replace(b'"next_cursor": "YQ"', b'"next_cursor": "YQ", "next_cursor": null'),
            raw.replace(b'"in_progress"', b"NaN"),
            response(schema_id="private canary schema")
        )

        for body in malformed:
            with self.subTest(size=len(body)):
                transport = Transport(reply(body))

                with self.assertRaises(ApiFailure) as failure:
                    await api(transport, "https://api.example").execution_attempts(state="in_progress", limit=1, cursor=None)

                yield (body, transport, failure.exception)

    async def test_bad_wire_retains_evidence_and_no_mutation_uncertainty(self):
        async for (body, transport, failure) in self._bad_wire_failures():
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(failure.response.body, body)
            self.assertFalse(failure.attempt_mutation_may_have_committed)

    async def test_wire_failures_retain_native_parser_causes(self):
        for (body, parser_error) in ((b"\xff", UnicodeDecodeError), (b"{", json.JSONDecodeError)):
            with self.subTest(parser_error=parser_error):
                transport = Transport(reply(body))

                with self.assertRaises(ApiFailure) as failure:
                    await api(transport, "https://api.example").execution_attempts(state="in_progress", limit=1, cursor=None)

                self.assertIsInstance(failure.exception.cause.__cause__, parser_error)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_bad_wire_diagnostics_omit_response_content(self):
        async for (_body, _transport, failure) in self._bad_wire_failures():
            for rendered in (str(failure), repr(failure), "".join(traceback.format_exception(failure))):
                self.assertNotIn("private canary", rendered)
