"""Independent lanes cannot concurrently own the same remote Attempt."""

import unittest
from dataclasses import replace

from provider_core.attempt_request import start_body
from provider_core.ownership import AttemptClaims, OwnershipRefused
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF


class AttemptOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_key_excludes_lanes_and_binds_remote_identity_without_release(self):
        claims = AttemptClaims()
        pending = record()
        claim = claims.claim_start(pending)
        other_lane = replace(pending, scope=replace(scope(), consumer_id="other", generation_id="other"))
        with self.assertRaises(OwnershipRefused):
            claims.claim_start(other_lane)
        other_job = replace(
            pending, input=replace(pending.input, job_ref="job:other"), body=start_body("job:other", "retained:one")
        )
        with self.assertRaises(OwnershipRefused):
            claims.claim_start(other_job)
        independent = claims.claim_start(record("independent"))
        independent.release()
        owner = object()
        claim.begin(owner)
        claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
        claim.require(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim_start(pending)
        claim.finish(owner)
        claim.release()
        claims.claim_start(pending).release()
        claims.claim(scope(), ATTEMPT_REF).release()

    async def test_remote_binding_conflict_preserves_both_original_claims(self):
        claims = AttemptClaims()
        existing = claims.claim(scope(), ATTEMPT_REF)
        pending = record()
        starting = claims.claim_start(pending)
        owner = object()
        starting.begin(owner)
        with self.assertRaises(OwnershipRefused):
            starting.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
        self.assertTrue(existing.active)
        starting.require_start(pending)
        starting.finish(owner)
        starting.release()
        existing.require(scope(), ATTEMPT_REF)
        existing.release()

    async def test_lane_generation_differences_do_not_partition_remote_attempt_ownership(self):
        claims = AttemptClaims()
        first = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim(replace(scope(), consumer_id="another_lane", generation_id="another_generation"), ATTEMPT_REF)
        owner = object()
        first.begin(owner)
        with self.assertRaises(OwnershipRefused):
            first.release()
        with self.assertRaises(OwnershipRefused):
            first.finish(object())
        first.finish(owner)
        first.release()
        second = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            first.release()
        self.assertTrue(second.active)
        second.release()
