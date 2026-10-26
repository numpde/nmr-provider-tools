"""Signed terminal readback reconciles compact control without producer science."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import EvidenceDigest
from provider_core.recovery import AttemptRecovery, ObservedAttempt, RecoveryFailed
from provider_core.transport import HttpTransport
from tests.fakes.api import api
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import observation
from tests.fakes.checkpoint_execution import RecordingDelivery


class CheckpointRecoveryTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_read_reply_retains_entry_then_explicit_read_reconciles_without_post(self):
        async with api_tls_peer("success", response_bodies=[None, observation(state="failed")]) as peer:
            with tempfile.TemporaryDirectory() as directory:
                selected_scope = replace(scope(), api_origin=peer.origin)
                store = CheckpointStore(Path(directory), scope=selected_scope, limits=CheckpointLimits(2, 300000), operation_seconds=1)
                await store.open()
                try:
                    pending = replace(record(), scope=selected_scope)
                    await store.retain_start(pending)
                    ack = await store.acknowledge_start(pending, admission())
                    entry = await store.mark_entered(ack, EvidenceDigest("candidate.v1", "sha256:" + "b" * 64, 8))
                    claim = AttemptClaims().claim_record(entry)
                    relay = RecordingDelivery()
                    relay.endpoint = replace(relay.endpoint, api_origin=peer.origin)
                    client = api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2), peer.origin)
                    with self.assertRaises(RecoveryFailed):
                        await AttemptRecovery(client, store, claim, delivery=relay).recover(entry)
                    self.assertEqual(await store.inventory(), (entry,))
                    self.assertEqual(len(peer.requests), 1)
                    self.assertTrue(client.retirement_confirmed)
                    result = await AttemptRecovery(client, store, claim, delivery=relay).recover(entry)
                    self.assertIs(type(result), ObservedAttempt)
                    self.assertEqual(result.record, entry)
                    self.assertEqual(result.observation.state, "failed")
                    self.assertEqual(await store.inventory(), ())
                    self.assertEqual(len(peer.requests), 2)
                    for request, body in zip(peer.requests, peer.request_bodies, strict=True):
                        self.assertTrue(request.startswith(b"GET "))
                        self.assertEqual(body, b"")
                        verify_signed_request(request, body, origin=peer.origin, key=client.identity.private_key)
                    self.assertEqual(relay.items, [])
                    claim.release()
                    await client.retire()
                finally:
                    await store.close()
