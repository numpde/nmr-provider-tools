"""Live capacity keeps held Jobs excluded until their actual owners can release."""

import asyncio
from dataclasses import replace
import unittest

from provider_core.attempt_request import start_body
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull, OwnershipRefused
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF


class Owner:
    def __init__(self):
        self.release_ready = False
        self.terminal_ready = False

    def require_claim_release_ready(self, claim):
        if not self.release_ready:
            raise OwnershipRefused("Original lifecycle remains unresolved")

    def require_terminal_release_ready(self, claim):
        if not self.terminal_ready:
            raise OwnershipRefused("No matching delivery acknowledgement")


def selected(job, key):
    pending = record(key)
    return replace(pending, input=replace(pending.input, job_ref=job), body=start_body(job, key))


class LiveOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_job_or_key_excludes_other_lanes_but_unrelated_work_can_run(self):
        claims, owner = AttemptClaims.live(), Owner()
        pending = selected("job:a", "key:a")
        first = claims.claim_live(pending, lifecycle_owner=owner)
        other_lane = replace(selected("job:a", "key:b"), scope=replace(scope(), consumer_id="other"))
        for competing in (other_lane, selected("job:b", "key:a")):
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(competing, lifecycle_owner=Owner())
        other = claims.claim_live(selected("job:b", "key:b"), lifecycle_owner=Owner())
        self.assertTrue(first.active)
        self.assertTrue(other.active)
        self.assertIs(first.lifecycle_owner, owner)
        self.assertIn(owner, claims.live_owners())
        self.assertEqual(2, len(claims.live_owners()))
        self.assertTrue(claims.owns_job(other_lane.scope, "job:a"))
        self.assertFalse(claims.owns_job(scope(), "job:c"))
        await asyncio.sleep(0)
        with self.assertRaises(OwnershipRefused):
            claims.claim_live(pending, lifecycle_owner=Owner())

    async def test_each_capacity_refusal_acquires_no_partial_job_or_key(self):
        for limits in (
            LiveAttemptLimits(maximum_attempts=1),
            LiveAttemptLimits(maximum_control_bytes=131072),
            LiveAttemptLimits(maximum_pending_terminal_bytes=2097152),
        ):
            with self.subTest(limits=limits):
                claims, owner = AttemptClaims.live(limits=limits), Owner()
                first = claims.claim_live(selected("job:a", "key:a"), lifecycle_owner=owner)
                pending = selected("job:b", "key:b")
                with self.assertRaises(LiveCapacityFull):
                    claims.claim_live(pending, lifecycle_owner=Owner())
                owner.release_ready = True
                first.release(owner=owner)
                admitted = claims.claim_live(pending, lifecycle_owner=Owner())
                self.assertTrue(admitted.active)

    async def test_duplicate_identity_is_not_misreported_as_capacity_backpressure(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        claims.claim_live(selected("job:a", "key:a"), lifecycle_owner=Owner())
        with self.assertRaises(OwnershipRefused) as refused:
            claims.claim_live(selected("job:a", "key:b"), lifecycle_owner=Owner())
        self.assertIs(type(refused.exception), OwnershipRefused)
        with self.assertRaises(LiveCapacityFull):
            claims.claim_live(selected("job:b", "key:b"), lifecycle_owner=Owner())

    async def test_ack_releases_terminal_allowance_but_not_job_or_control_ownership(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_pending_terminal_bytes=2097152))
        owner, borrower = Owner(), Owner()
        first = claims.claim_live(selected("job:a", "key:a"), lifecycle_owner=owner)
        first.begin(borrower)
        with self.assertRaises(OwnershipRefused):
            first.release_terminal(owner=owner)
        with self.assertRaises(OwnershipRefused):
            first.release_terminal(owner=borrower)
        borrower.terminal_ready = True
        first.release_terminal(owner=borrower)
        other = claims.claim_live(selected("job:b", "key:b"), lifecycle_owner=Owner())
        self.assertTrue(other.active)
        with self.assertRaises(OwnershipRefused):
            first.release(owner=owner)
        first.finish(borrower)
        with self.assertRaises(OwnershipRefused):
            first.release(owner=owner)
        with self.assertRaises(OwnershipRefused):
            claims.claim_live(selected("job:a", "key:c"), lifecycle_owner=Owner())
        owner.release_ready = True
        with self.assertRaises(OwnershipRefused):
            first.release(owner=Owner())
        first.release(owner=owner)
        self.assertFalse(first.active)
        self.assertNotIn(owner, claims.live_owners())
        self.assertFalse(claims.owns_job(scope(), "job:a"))

    async def test_attempt_binding_adds_exclusion_without_another_capacity_charge(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=2))
        first = claims.claim_live(selected("job:a", "key:a"), lifecycle_owner=Owner())
        borrower = Owner()
        first.begin(borrower)
        first.bind_attempt(scope(), ATTEMPT_REF, owner=borrower)
        second = claims.claim_live(selected("job:b", "key:b"), lifecycle_owner=Owner())
        second.begin(borrower)
        with self.assertRaises(OwnershipRefused):
            second.bind_attempt(scope(), ATTEMPT_REF, owner=borrower)
        first.require(scope(), ATTEMPT_REF)
        self.assertTrue(second.active)

    async def test_changed_pending_is_not_an_exact_live_selection(self):
        pending = record()
        claim = AttemptClaims.live().claim_live(pending, lifecycle_owner=Owner())
        for changed in (
            replace(pending, scope=replace(scope(), generation_id="other")),
            replace(pending, input=replace(pending.input, job_ref="job:other"),
                    body=start_body("job:other", "retained:one")),
            replace(pending, body=pending.body + b" "),
        ):
            with self.assertRaises(OwnershipRefused):
                claim.require_start(changed)
        claim.require_start(pending)

    async def test_oversized_pending_refuses_before_capacity_is_reserved(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
        pending = replace(record(), scope=replace(scope(), api_origin="https://" + "a" * 65536 + ".example"))
        with self.assertRaises(ValueError):
            claims.claim_live(pending, lifecycle_owner=Owner())
        self.assertTrue(claims.claim_live(record(), lifecycle_owner=Owner()).active)


    async def test_terminal_grants_cover_fail_and_respect_the_wire_ceiling(self):
        claims = AttemptClaims.live()
        for allowance in (True, 0, 16383, 2097153):
            with self.assertRaises(ValueError):
                claims.claim_live(record(), lifecycle_owner=Owner(), maximum_terminal_bytes=allowance)
        claim = claims.claim_live(record(), lifecycle_owner=Owner(), maximum_terminal_bytes=16384)
        self.assertEqual(16384, claim.maximum_terminal_bytes)
