"""Discovery refuses protocol drift and substitutions before input/science work."""

import traceback
import unittest
import os

from provider_core.api_errors import ApiFailure
from tests.fakes.api import Transport, reply
from tests.fakes.job_feed import response
from tests.fakes.api import api


class JobFeedFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_oversized_or_malformed_wire_is_never_returned_as_a_page(self):
        raw = response()

        for malformed in (
            raw + b" " * 65536,
            raw.replace(b'"next_cursor": "YQ"', b'"next_cursor": "YQ", "next_cursor": null'),
            b"\xff",
            b"{",
            raw.replace(b'"input_byte_length": 20', b'"input_byte_length": NaN')
        ):
            with self.subTest(size=len(malformed)):
                transport = Transport(reply(malformed))

                with self.assertRaises(ApiFailure):
                    await api(transport, "https://api.example").jobs(
                        analysis_kind_ref="nmr_predict",
                        has_provider_execution_attempt=False,
                        limit=1,
                        cursor=None
                    )

                self.assertEqual(len(transport.calls), 1)

    async def test_bad_query_dispatches_nothing(self):
        for (field, bad, error_type) in (
            ("analysis_kind_ref", "wrong kind", ValueError),
            ("limit", True, TypeError),
            ("limit", 0, ValueError),
            ("limit", 101, ValueError),
            ("has_provider_execution_attempt", 0, TypeError),
            ("cursor", "YQ=", ValueError),
            ("cursor", "YR", ValueError),
            ("cursor", "a", ValueError),
            ("cursor", "", ValueError),
        ):
            with self.subTest(field=field, bad=bad):
                arguments = {"analysis_kind_ref": "nmr_predict", "has_provider_execution_attempt": False, "limit": 1, "cursor": None}
                arguments[field] = bad
                transport = Transport()

                with self.assertRaises(error_type):
                    await api(transport, "https://api.example").jobs(**arguments)

                self.assertEqual(transport.calls, [])

    async def _wire_failure(self):
        raw = response().replace(b"nmr.provider.jobs.list.response.v1", b"private canary schema")
        transport = Transport(reply(raw))

        with self.assertRaises(ApiFailure) as failure:
            await api(transport, "https://api.example").jobs(
                analysis_kind_ref="nmr_predict",
                has_provider_execution_attempt=False,
                limit=1,
                cursor=None
            )

        return (failure.exception, raw)

    async def test_wire_failure_retains_response_and_read_effect_truth(self):
        (failure, raw) = await self._wire_failure()

        self.assertEqual(failure.response.body, raw)
        self.assertFalse(failure.attempt_mutation_may_have_committed)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_wire_failure_diagnostics_omit_response_content(self):
        (failure, _raw) = await self._wire_failure()

        self.assertNotIn("private canary", str(failure))
        self.assertNotIn("private canary", repr(failure))
        self.assertNotIn("private canary", "".join(traceback.format_exception(failure)))
