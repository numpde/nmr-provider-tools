"""Invalid Upload observations never become transfer authority or hidden retries."""

import asyncio
import traceback
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership


class UploadApiFailureTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def test_membership_rejects_substitution_order_duplicates_and_bad_fields(self):
        cases = [
            membership(job_ref="job:foreign"),
            membership(extra=True),
            membership(uploads=[member(), member()]),
            membership(uploads=[member(upload_ref="upload:sha256:" + "c" * 64), member()]),
            membership(uploads=[member(byte_length=True)]),
            membership(uploads=[member(description="bad\x00")]),
            membership(uploads=[member(description="\ud800")]),
            membership(uploads=[member()] * 65),
        ]
        for document in cases:
            transport = Transport(reply(encoded(document)))
            with self.subTest(document=document), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).uploads("job:example")
            self.assertFalse(caught.exception.issuance_may_have_committed)
            self.assertEqual(len(transport.calls), 1)

    async def test_bad_grants_preserve_possible_issuance_without_disclosing_bearer(self):
        for changes in (
            {"upload_ref": "upload:sha256:" + "c" * 64},
            {"method": "PUT"},
            {"content_hash": None},
            {"byte_length": True},
            {"expires_at": "2026-02-30T00:00:00Z"},
            {"expires_at": "2026-10-19T00:00:00.000000Z"},
            {"capability": ""},
            {"download_url": "http://store.example"},
            {"extra": True},
        ):
            transport = Transport(reply(encoded(grant(**changes))))
            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).read_grant("job:example", UPLOAD_REF)
            self.assertTrue(caught.exception.issuance_may_have_committed)
            self.assertNotIn("private-bearer-canary", "".join(traceback.format_exception(caught.exception)))
            self.assertEqual(len(transport.calls), 1)

    async def test_grant_http_io_and_cancel_failures_are_uncertain_and_never_retried(self):
        failure = ExchangeFailed("Cannot complete HTTPS exchange")
        for result in (reply(b"unavailable", status=503), failure, asyncio.CancelledError()):
            transport = Transport(result)
            transport.last_failure = failure
            api = self.api(transport)
            with (
                self.subTest(result=type(result).__name__),
                self.assertRaises(asyncio.CancelledError if isinstance(result, asyncio.CancelledError) else ApiFailure),
            ):
                await api.read_grant("job:example", UPLOAD_REF)
            self.assertTrue(api.last_failure.issuance_may_have_committed)
            self.assertEqual(len(transport.calls), 1)

    async def test_bad_request_references_do_not_send_anything(self):
        transport = Transport()
        api = self.api(transport)
        with self.assertRaises(ValueError):
            await api.uploads("job:../foreign")
        with self.assertRaises(ValueError):
            await api.read_grant("job:example", "upload:bad/target")
        self.assertEqual(transport.calls, [])

    async def test_only_reviewed_no_change_problem_resolves_this_issuance(self):
        cases = (
            (401, "authentication-failed", "signature_expired", False),
            (403, "authorization-denied", "authorization_denied", True),
            (404, "not-found", "resource_not_found", True),
            (409, "operation-conflict", "upload_read_not_finalized", False),
            (409, "operation-conflict", "project_purge_in_progress", False),
        )
        for status, kind, code, uncertain in cases:
            document = {
                "type": "urn:nmr-api:problem:" + kind,
                "title": "Private title",
                "status": status,
                "instance": "urn:nmr-api:request:test",
                "request_id": "request:test",
                "code": code,
                "detail": "private-detail-canary",
            }
            transport = Transport(
                reply(encoded(document), status=status, headers=response_headers(status=status, request_id=b"request:test"))
            )
            with self.subTest(code=code), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).read_grant("job:example", UPLOAD_REF)
            self.assertEqual(caught.exception.issuance_may_have_committed, uncertain)
            self.assertEqual(caught.exception.problem_code, code)
            self.assertNotIn("private-detail-canary", str(caught.exception))
            self.assertEqual(len(transport.calls), 1)

    async def test_malformed_duplicate_and_overlimit_grant_json_retains_uncertainty(self):
        valid = encoded(grant())
        for raw in (b"\xff", b"{", valid[:-1] + b',"method":"GET"}', valid[:-1] + b',"unexpected":NaN}', valid + b" " * 65536):
            transport = Transport(reply(raw))
            with self.subTest(raw_length=len(raw)), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).read_grant("job:example", UPLOAD_REF)
            self.assertTrue(caught.exception.issuance_may_have_committed)
            self.assertEqual(len(transport.calls), 1)
