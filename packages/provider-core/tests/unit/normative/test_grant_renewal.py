"""Fresh grant policy belongs to the exact settled issuance and selected Upload."""

import httpcore
import unittest
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed, HttpResponseHead
from tests.fakes.api import Transport, io_failure, reply, response_headers
from tests.fakes.uploads import UPLOAD_REF, encoded, membership


def problem(status, kind, code):
    return reply(encoded({
        "type": "urn:nmr-api:problem:" + kind, "title": "Private title",
        "status": status, "instance": "urn:nmr-api:request:test", "request_id": "request:test",
        "code": code, "detail": "private-detail-canary",
    }), status=status, headers=response_headers(status=status, request_id=b"request:test"))


class GrantRenewalTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                           transport, expected_topology="dev-local")

    async def failure(self, api):
        with self.assertRaises(ApiFailure) as caught:
            await api.read_grant("job:example", UPLOAD_REF)
        return caught.exception

    # silent-defaults: allow-contract -- The normal fixture selects these exact references; foreign-identity cases override them explicitly.
    def permitted(self, api, failure, *, job="job:example", upload=UPLOAD_REF):
        return api.can_renew_read_grant(failure, job_ref=job, upload_ref=upload)

    async def test_only_pending_conflict_permits_fresh_issuance_without_implicit_retry(self):
        for code in ("upload_read_not_finalized", "upload_read_removal_scheduled",
                     "project_purge_in_progress", "operation_conflict"):
            transport = Transport(problem(409, "operation-conflict", code))
            api = self.api(transport)
            failure = await self.failure(api)
            self.assertEqual(self.permitted(api, failure), code == "upload_read_not_finalized", code)
            if code == "upload_read_not_finalized":
                self.assertFalse(failure.issuance_may_have_committed)
            self.assertFalse(api.can_retry_read(failure))
            self.assertEqual(len(transport.calls), 1)

    async def test_exact_route_method_upload_and_current_failure_are_required(self):
        pending = problem(409, "operation-conflict", "upload_read_not_finalized")
        transport = Transport(pending, reply(encoded(membership())))
        api = self.api(transport)
        failure = await self.failure(api)
        self.assertTrue(self.permitted(api, failure))
        self.assertFalse(self.permitted(api, failure, job="job:foreign"))
        self.assertFalse(self.permitted(api, failure, upload="upload:sha256:" + "c" * 64))
        foreign = self.api(Transport(pending))
        self.assertFalse(self.permitted(foreign, failure))
        await api.uploads("job:example")
        self.assertFalse(self.permitted(api, failure))
        other_route = self.api(Transport(pending))
        with self.assertRaises(ApiFailure) as caught:
            await other_route.uploads("job:example")
        self.assertFalse(self.permitted(other_route, caught.exception))

    async def test_unsettled_issuance_requires_original_transport_retirement(self):
        transport = Transport(io_failure("read interrupted", stage="read", primary=httpcore.ReadTimeout()))
        api = self.api(transport)
        failure = await self.failure(api)
        transport.retirement_confirmed = False
        self.assertFalse(self.permitted(api, failure))
        await api.retire()
        self.assertTrue(self.permitted(api, failure))
        self.assertTrue(failure.issuance_may_have_committed)
        self.assertEqual(len(transport.calls), 1)

    async def test_existing_signature_and_gateway_evidence_still_permit_new_grant(self):
        for response in (problem(401, "authentication-failed", "signature_expired"), reply(b"unavailable", status=503)):
            transport = Transport(response)
            api = self.api(transport)
            failure = await self.failure(api)
            self.assertTrue(self.permitted(api, failure))
            self.assertEqual(len(transport.calls), 1)

    async def test_pending_requires_complete_unique_headers_and_body_correlation(self):
        pending = problem(409, "operation-conflict", "upload_read_not_finalized")
        headers = pending.headers
        # repo-check: allow-tuple-generator -- HttpResponse owns immutable ordered wire headers; retain that shape while omitting the correlation header.
        without_request = tuple((name, value) for name, value in headers if name.lower() != b"x-request-id")
        # repo-check: allow-tuple-generator -- This malformed topology specimen must retain the same immutable ordered header contract.
        without_topology = tuple((name, value) for name, value in headers if name.lower() != b"nmr-api-topology")
        responses = (
            replace(pending, headers=without_request),
            replace(pending, headers=without_request + ((b"x-request-id", b"request:foreign"),)),
            replace(pending, headers=headers + ((b"x-request-id", b"request:test"),)),
            replace(pending, headers=without_topology),
            replace(pending, headers=without_topology + ((b"nmr-api-topology", b"web"),)),
            replace(pending, headers=headers + ((b"nmr-api-topology", b"dev-local"),)),
            replace(pending, body=pending.body[:-1] + b',"code":"upload_read_not_finalized"}'),
            io_failure("pending body was interrupted", stage="read", primary=httpcore.ReadTimeout(),
                           response_head=HttpResponseHead(pending.status, headers)),
        )
        for index, response in enumerate(responses):
            with self.subTest(case=index):
                api = self.api(Transport(response))
                failure = await self.failure(api)
                self.assertIsNone(failure.problem_code)
                self.assertFalse(self.permitted(api, failure))

    async def test_diagnosis_and_undeclared_rate_status_never_grant_renewal(self):
        for status in (500, 429):
            with self.subTest(status=status):
                api = self.api(Transport(reply(b"unavailable", status=status)))
                failure = await self.failure(api)
                self.assertFalse(self.permitted(api, failure))
                self.assertTrue(failure.issuance_may_have_committed)

    async def test_unknown_protocol_and_wrong_environment_do_not_grant_renewal(self):
        for response in (
            ExchangeFailed("malformed response", primary=httpcore.RemoteProtocolError()),
            reply(b"unavailable", status=503, headers=response_headers(status=503, topology=b"web")),
            problem(403, "authorization-denied", "authorization_denied"),
            problem(404, "not-found", "resource_not_found"),
        ):
            api = self.api(Transport(response))
            self.assertFalse(self.permitted(api, await self.failure(api)))
