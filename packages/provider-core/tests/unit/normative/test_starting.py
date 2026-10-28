"""A returned start acknowledgement remains owned; it does not grant execution."""

from dataclasses import replace
import unittest

from provider_core.attempt_record import LocalExecutionPhase
from provider_core.attempt_request import start_body
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.starting import AttemptStarter, StartFailed, StartRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.api import api
from tests.fakes.execution import LiveAttemptProbe


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

    async def test_remote_conflict_retains_received_admission_and_both_live_claims(self):
        claims = AttemptClaims.live()
        pending = record()
        other = replace(record("first"), input=replace(pending.input, job_ref="job:other"),
                        body=start_body("job:other", "first"))
        existing = claims.claim_live(other, lifecycle_owner=LiveAttemptProbe())
        owner = object()
        existing.begin(owner)
        existing.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
        existing.finish(owner)
        claim = claims.claim_live(pending, lifecycle_owner=LiveAttemptProbe())
        transport = Transport(reply(start_response()))
        starter = AttemptStarter.live(api(transport, scope().api_origin), claim, scope=scope())
        with self.assertRaises(StartFailed) as failed:
            await starter.start(pending)
        acknowledgement = failed.exception.acknowledgement
        self.assertEqual(ATTEMPT_REF, acknowledgement.admission.execution_attempt_ref)
        self.assertEqual(pending, acknowledgement.start)
        self.assertTrue(existing.active)
        self.assertTrue(claim.active)
        self.assertIs(starter, claim.operation_owner)
        with self.assertRaises(StartFailed) as unsettled:
            await starter.settle()
        self.assertIs(acknowledgement, unsettled.exception.acknowledgement)
        self.assertIsInstance(unsettled.exception.settlement_failures[-1], StartRefused)
        with self.assertRaises(StartRefused):
            await starter.resume(pending)
        self.assertEqual(1, len(transport.calls))
