"""Signed inventory observations feed a separate, identity-bound current read."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_inventory import attempt, response
from tests.fakes.attempts import observation


class AttemptInventoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reordered_mixed_kind_inventory_binds_separate_current_read_to_selected_attempt(self):
        first = attempt()
        selected = attempt(analysis_kind_ref="nmr_rank", job_ref="job:selected",
                           execution_attempt_ref="execution_attempt:sha256:" + "b" * 64,
                           provider_attempt_key="other-start:02")
        for order in ((first, selected), (selected, first)):
            key = Ed25519PrivateKey.generate()
            readback = observation(execution_attempt_ref=selected["execution_attempt_ref"],
                                   job_ref=selected["job_ref"], job_state="closed")
            async with api_tls_peer("success", response_bodies=[response(attempts=order), readback]) as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
                client = ProviderAPI(
                    SigningIdentity(peer.origin, "credential:test", key), transport, expected_topology="dev-local"
                )
                page = await client.execution_attempts(state="in_progress", limit=2, cursor=None)
                self.assertEqual([item.job_ref for item in page.attempts], [item["job_ref"] for item in order])
                item = next(item for item in page.attempts if item.job_ref == selected["job_ref"])
                current = await client.read_attempt(item.execution_attempt_ref, job_ref=item.job_ref)
                self.assertEqual((current.execution_attempt_ref, current.job_ref, current.state, current.job_state),
                                 (selected["execution_attempt_ref"], selected["job_ref"], "in_progress", "closed"))
                for request, target in zip(peer.requests, (
                    b"/provider/v1/execution-attempts?state=in_progress&limit=2",
                    b"/provider/v1/execution-attempts/" + selected["execution_attempt_ref"].encode(),
                ), strict=True):
                    self.assertEqual(request.split(b"\r\n")[0], b"GET " + target + b" HTTP/1.1")
                    headers = {name.lower(): value.strip() for line in request.split(b"\r\n")[1:] if b":" in line
                               for name, value in [line.split(b":", 1)]}
                    path, _, query = target.partition(b"?")
                    base = b"\n".join([b'"@method": GET', b'"@authority": ' + peer.origin.removeprefix("https://").encode(),
                                        b'"@path": ' + path, b'"@query": ?' + query,
                                        b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1]])
                    key.public_key().verify(parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base)
                    self.assertNotIn(b"content-digest", headers)
                self.assertEqual(peer.request_bodies, [b"", b""])
                await transport.retire()

    async def test_cancelled_inventory_retires_original_transport_without_mutation_or_retry(self):
        async with api_tls_peer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            client = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            owner = object()
            task = asyncio.create_task(client.execution_attempts(state="in_progress", limit=1, cursor=None, owner=owner))
            await asyncio.wait_for(peer.received.wait(), 1)
            self.assertIs(client.pending_owner, owner)
            with self.assertRaises(ExchangeFailed):
                await client.retire(owner=object())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(client.last_failure.cause.primary, asyncio.CancelledError)
            self.assertFalse(client.last_failure.attempt_mutation_may_have_committed)
            self.assertFalse(client.last_failure.issuance_may_have_committed)
            await client.retire(owner=owner)
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertEqual(len(peer.requests), 1)
