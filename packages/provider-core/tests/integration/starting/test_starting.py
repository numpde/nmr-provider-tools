"""A lost start reply retains the exact restart obligation across storage reopen."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.api import api

from provider_core.attempt_record import LocalExecutionPhase
from provider_core.checkpoint_store import CheckpointStore, CheckpointLimits
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from provider_core.ownership import AttemptClaims
from provider_core.recovery import AttemptRecovery, RecoveryRefused
from provider_core.starting import AttemptStarter, StartFailed
from provider_core.transport import HttpTransport


class StartingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_start_reply_reopens_and_replays_original_bytes(self):
        retained_at_dispatch = []
        responses = iter((None, start_response(replayed=True)))

        async def observe_start(request, body):
            retained_at_dispatch.append(await active_store.inventory())
            return next(responses)

        async with api_tls_peer("success", response_factory=observe_start) as peer:
            recovery_scope = replace(scope(), api_origin=peer.origin)
            pending = replace(record(), scope=recovery_scope)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            claims = AttemptClaims()
            with tempfile.TemporaryDirectory() as directory:
                store = CheckpointStore(
                    Path(directory), scope=recovery_scope, limits=CheckpointLimits(1, 150000), operation_seconds=1
                )
                await store.open()
                active_store = store
                claim = claims.claim_start(pending)
                try:
                    with self.assertRaises(StartFailed) as lost:
                        await AttemptStarter(client, store, claim).start(pending)
                    self.assertTrue(lost.exception.api_failure.attempt_mutation_may_have_committed)
                    self.assertEqual(await store.inventory(), (pending,))
                    self.assertEqual(peer.request_bodies, [pending.body])
                    claim.release()
                finally:
                    await store.close()
                reopened = CheckpointStore(
                    Path(directory), scope=recovery_scope, limits=CheckpointLimits(1, 150000), operation_seconds=1
                )
                await reopened.open()
                active_store = reopened
                try:
                    (retained,) = await reopened.inventory()
                    (claim,) = claims.claim_records((retained,))
                    delivery = RecordingDelivery()
                    delivery.endpoint = replace(delivery.endpoint, api_origin=reopened.scope.api_origin)
                    recovery = AttemptRecovery(client, reopened, claim, delivery=delivery)
                    acknowledged = await recovery.recover(retained)
                    self.assertEqual(await reopened.inventory(), (AcknowledgedCheckpoint(pending, acknowledged.admission),))
                    self.assertTrue(acknowledged.admission.replayed)
                    self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
                    claim.require(recovery_scope, ATTEMPT_REF)
                    self.assertEqual(peer.request_bodies, [pending.body, pending.body])
                    self.assertEqual(retained_at_dispatch, [(pending,), (pending,)])
                    for request in peer.requests:
                        verify_signed_request(request, pending.body, origin=peer.origin, key=client.identity.private_key)
                    self.assertNotEqual(peer.requests[0], peer.requests[1])
                    with self.assertRaises(RecoveryRefused):
                        await recovery.recover(retained)
                    self.assertEqual(len(peer.requests), 2)
                    claim.release()
                finally:
                    await reopened.close()
            await transport.retire()
