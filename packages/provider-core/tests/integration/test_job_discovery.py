"""Authenticated discovery supplies the identities consumed by exact input reads."""

import asyncio
import hashlib
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.job_feed import page as feed_page
from tests.fakes.job_feed import response as feed_response
from tests.fakes.job_input import response as input_response


class JobDiscoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reordered_distinct_jobs_keep_the_selected_identity_and_payload_together(self):
        payload = b"selected second Job instructions"
        original = feed_page()["jobs"][0]
        other = {**original, "job_ref": "job:second", "input_byte_length": len(payload),
                 "input_fingerprint": "sha256:" + hashlib.sha256(payload).hexdigest()}
        for order in ((original, other), (other, original)):
            document = feed_page()
            document["jobs"] = order
            replies = [json.dumps(document).encode(), input_response(payload, job_ref="job:second")]
            async with api_tls_peer("success", response_bodies=replies) as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
                client = ProviderAPI(
                    SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                    transport,
                    expected_topology="dev-local",
                )
                page = await client.jobs(analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=2, cursor=None)
                self.assertEqual([item.input.job_ref for item in page.jobs], [item["job_ref"] for item in order])
                selected = next(item for item in page.jobs if item.input.job_ref == "job:second")
                verified = await client.job_input(selected.input, analysis_kind_ref=selected.analysis_kind_ref)
                self.assertEqual(verified.specification_bytes, payload)
                self.assertEqual(verified.identity, selected.input)
                self.assertTrue(peer.requests[1].startswith(b"GET /provider/v1/jobs/job:second/input?"))
                await transport.retire()

    async def test_verified_input_uses_the_identity_received_from_signed_tls_discovery(self):
        key = Ed25519PrivateKey.generate()
        async with api_tls_peer("success", response_bodies=[feed_response(), input_response()]) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = ProviderAPI(SigningIdentity(peer.origin, "credential:test", key), transport, expected_topology="dev-local")
            page = await client.jobs(analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=1, cursor=None)
            selected, = page.jobs
            verified = await client.job_input(selected.input, analysis_kind_ref=selected.analysis_kind_ref)
            self.assertEqual(verified.identity, selected.input)
            self.assertEqual(verified.specification_bytes, b"private instructions")
            self.assertEqual(len(peer.requests), 2)
            for request, expected_target in zip(peer.requests, (
                b"/provider/v1/jobs?analysis_kind_ref=nmr_predict&has_provider_execution_attempt=false&limit=1",
                b"/provider/v1/jobs/job:example/input?analysis_kind_ref=nmr_predict",
            ), strict=True):
                self.assertEqual(request.split(b"\r\n")[0], b"GET " + expected_target + b" HTTP/1.1")
                headers = {name.lower(): value.strip() for line in request.split(b"\r\n")[1:] if b":" in line
                           for name, value in [line.split(b":", 1)]}
                path, query = expected_target.split(b"?", 1)
                base = b"\n".join([b'"@method": GET', b'"@authority": ' + peer.origin.removeprefix("https://").encode(),
                                    b'"@path": ' + path, b'"@query": ?' + query,
                                    b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1]])
                key.public_key().verify(parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base)
                self.assertNotIn(b"content-digest", headers)
            self.assertEqual(peer.request_bodies, [b"", b""])
            await transport.retire()

    async def test_cancelled_discovery_keeps_read_context_until_original_transport_retires(self):
        async with api_tls_peer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            client = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            owner = object()
            task = asyncio.create_task(client.jobs(analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False,
                                                   limit=1, cursor=None, owner=owner))
            await asyncio.wait_for(peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(client.last_failure.cause.primary, asyncio.CancelledError)
            self.assertFalse(client.last_failure.attempt_mutation_may_have_committed)
            await client.retire(owner=owner)
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertEqual(len(peer.requests), 1)
