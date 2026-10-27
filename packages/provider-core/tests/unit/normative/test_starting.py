"""A returned start acknowledgement remains owned; it does not grant execution."""

import tempfile
import unittest
from pathlib import Path

from provider_core.attempt_record import LocalExecutionPhase
from provider_core.checkpoint_store import CheckpointStore, CheckpointLimits
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.starting import AttemptStarter
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.api import api


class StartingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_start_binds_the_actual_acknowledgement_without_checkpoint_storage(self):
        pending = record()
        claims = AttemptClaims.live()
        claim = claims.claim_live(pending, lifecycle_owner=object())
        transport = Transport(reply(start_response()), reply(start_response(replayed=True)), reply(start_response(replayed=True)))
        starter = AttemptStarter.live(api(transport, scope().api_origin), claim, scope=scope())
        acknowledged = await starter.start(pending)
        self.assertEqual(pending, acknowledged.start)
        self.assertEqual(ATTEMPT_REF, acknowledged.admission.execution_attempt_ref)
        self.assertIs(LocalExecutionPhase.NOT_ENTERED, acknowledged.local_phase)
        self.assertEqual(pending.body, transport.calls[0][0].content)
        claim.require(scope(), ATTEMPT_REF)
        self.assertIsNone(claim.operation_owner)
        self.assertTrue(claims.owns_job(scope(), pending.input.job_ref))
        replacement = AttemptStarter.live(starter.api, claim, scope=scope())
        for send in (starter.start, starter.resume, replacement.start, replacement.resume):
            with self.assertRaises(OwnershipRefused):
                await send(pending)
        self.assertEqual(1, len(transport.calls))

    async def test_live_start_refuses_a_durable_claim_without_a_capacity_reservation(self):
        claim = AttemptClaims().claim_start(record())
        transport = Transport(reply(start_response()))
        with self.assertRaises(OwnershipRefused):
            AttemptStarter.live(api(transport, scope().api_origin), claim, scope=scope())
        self.assertEqual([], transport.calls)

    async def test_fresh_and_terminal_replay_admissions_are_durable_and_keep_claims(self):
        for state in ("in_progress", "succeeded", "failed", "expired"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                pending = record()
                transport = Transport(reply(start_response(state=state, replayed=state != "in_progress")))
                claims = AttemptClaims()
                claim = claims.claim_start(pending)
                store = CheckpointStore(
                    Path(directory), scope=scope(), limits=CheckpointLimits(1, 150000), operation_seconds=1
                )
                await store.open()
                try:
                    acknowledged = await AttemptStarter(api(transport, scope().api_origin), store, claim).start(pending)
                    self.assertEqual(await store.inventory(), (AcknowledgedCheckpoint(pending, acknowledged.admission),))
                    self.assertEqual(acknowledged.admission.state, state)
                    self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
                    self.assertEqual(transport.calls[0][0].content, pending.body)
                    claim.require(scope(), ATTEMPT_REF)
                    with self.assertRaises(OwnershipRefused):
                        claims.claim_start(pending)
                    claim.release()
                finally:
                    await store.close()
