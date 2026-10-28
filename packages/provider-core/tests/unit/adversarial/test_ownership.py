"""Independent lanes cannot concurrently own the same remote Attempt."""

import unittest
from dataclasses import replace

from provider_core.attempt_request import start_body
from provider_core.ownership import AttemptClaims, OwnershipRefused
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.execution import LiveAttemptProbe


def reserve(claims, pending):
    return claims.claim_live(pending, lifecycle_owner=LiveAttemptProbe())


def bind(claim):
    owner = object()
    claim.begin(owner)
    try:
        claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
    finally:
        claim.finish(owner)


def another_job():
    pending = record("independent")
    return replace(pending, input=replace(pending.input, job_ref="job:other"), body=start_body("job:other", "independent"))


class AttemptOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_key_excludes_lanes_and_binds_remote_identity_without_release(self):
        claims = AttemptClaims.live()
        pending = record()
        claim = reserve(claims, pending)
        other_lane = replace(pending, scope=replace(scope(), consumer_id="other", generation_id="other"))
        with self.assertRaises(OwnershipRefused):
            reserve(claims, other_lane)
        other_job = replace(
            pending, input=replace(pending.input, job_ref="job:other"), body=start_body("job:other", "retained:one")
        )
        with self.assertRaises(OwnershipRefused):
            reserve(claims, other_job)
        independent = reserve(claims, another_job())
        independent.release(owner=independent.lifecycle_owner)
        owner = object()
        claim.begin(owner)
        claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
        claim.require(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            independent = reserve(claims, another_job())
            bind(independent)
        with self.assertRaises(OwnershipRefused):
            reserve(claims, pending)
        independent.release(owner=independent.lifecycle_owner)
        claim.finish(owner)
        claim.release(owner=claim.lifecycle_owner)
        replacement = reserve(claims, pending)
        replacement.release(owner=replacement.lifecycle_owner)

    async def test_remote_binding_conflict_preserves_both_original_claims(self):
        claims = AttemptClaims.live()
        existing = reserve(claims, another_job())
        bind(existing)
        pending = record()
        starting = reserve(claims, pending)
        owner = object()
        starting.begin(owner)
        with self.assertRaises(OwnershipRefused):
            starting.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
        self.assertTrue(existing.active)
        starting.require_start(pending)
        starting.finish(owner)
        starting.release(owner=starting.lifecycle_owner)
        existing.require(scope(), ATTEMPT_REF)
        existing.release(owner=existing.lifecycle_owner)

    async def test_lane_generation_differences_do_not_partition_remote_attempt_ownership(self):
        claims = AttemptClaims.live()
        first = reserve(claims, record())
        bind(first)
        pending = another_job()
        other = reserve(claims, replace(pending, scope=replace(scope(), consumer_id="another_lane", generation_id="another_generation")))
        with self.assertRaises(OwnershipRefused):
            bind(other)
        other.release(owner=other.lifecycle_owner)
        owner = object()
        first.begin(owner)
        with self.assertRaises(OwnershipRefused):
            first.release()
        with self.assertRaises(OwnershipRefused):
            first.finish(object())
        first.finish(owner)
        first.release(owner=first.lifecycle_owner)
        second = reserve(claims, record())
        bind(second)
        with self.assertRaises(OwnershipRefused):
            first.release()
        self.assertTrue(second.active)
        second.release(owner=second.lifecycle_owner)
