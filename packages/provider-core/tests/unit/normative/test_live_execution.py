"""Live science hands off exact bytes while retaining unfinished cleanup."""

import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock

from provider_core.attempt_request import start_body
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, OwnershipRefused
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.starting import AttemptStarter
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.live_analysis import LiveSession


class LiveOwner:
    def __init__(self):
        self.executor = None

    def require_execution_ready(self, claim, executor, acknowledged):
        if executor is not self.executor:
            raise ExecutionRefused("This is not the original executor retained by the lifecycle owner")


class LiveExecutionTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary live cases reserve the chosen 2 MiB wire ceiling; the oversized-result case declares only the FAIL allowance.
    async def setup_execution(self, preverified, *, terminal_bytes=2097152):
        pending = record()
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_pending_terminal_bytes=2097152))
        owner = LiveOwner()
        claim = claims.claim_live(pending, lifecycle_owner=owner, maximum_terminal_bytes=terminal_bytes)
        replies = [reply(start_response()), reply(observation())]
        verified = None
        if preverified:
            verified = verify_job_input(response(), feed=pending.input)
        else:
            replies.append(reply(response()))
        replies.extend([
            reply(progress_response()), reply(observation()), reply(progress_response(phase="running")),
            reply(observation()), reply(observation()),
        ])
        transport = Transport(*replies)
        client = api(transport, scope().api_origin)
        acknowledged = await AttemptStarter.live(client, claim, scope=scope()).start(pending)
        session, delivery = LiveSession(), RecordingDelivery()
        executor = AttemptExecutor.live(
            client, claim, session, scope=scope(), preverified_input=verified,
            delivery=delivery, on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=1, task_settle_seconds=0.05,
        )
        owner.executor = executor
        return claims, claim, acknowledged, executor, session, delivery, verified

    async def test_input_ordering_and_exact_delivery_without_storage(self):
        for preverified in (False, True):
            with self.subTest(preverified=preverified):
                claims, claim, acknowledged, executor, session, delivery, verified = await self.setup_execution(preverified)
                accepted = await executor.run(acknowledged)
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertEqual(["prepare", "execute", "retire"], session.events)
                if preverified:
                    self.assertIs(verified, session.instructions)
                self.assertEqual(delivery.items[0].body_sha256, accepted.selection.body_sha256)
                self.assertIsNone(claim.operation_owner)
                self.assertTrue(claims.owns_job(scope(), acknowledged.start.input.job_ref))
                self.assertTrue(executor.retirement_confirmed)

    async def test_replacement_executor_cannot_run_the_original_admission_again(self):
        claims, claim, acknowledged, executor, session, delivery, verified = await self.setup_execution(True)
        await executor.run(acknowledged)
        replacement = AttemptExecutor.live(
            executor.api, claim, LiveSession(), scope=scope(), preverified_input=verified,
            delivery=delivery, on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=1, task_settle_seconds=0.05,
        )
        with self.assertRaises(ExecutionRefused):
            await replacement.run(acknowledged)
        self.assertEqual(1, session.events.count("execute"))
        self.assertEqual(1, len(delivery.items))

    async def test_oversized_result_keeps_scientific_outcome_without_retaining_a_pending_report(self):
        claims, claim, acknowledged, executor, session, delivery, verified = await self.setup_execution(True, terminal_bytes=16384)
        session.content = b"x" * 20000
        with self.assertRaises(ExecutionFailed):
            await executor.run(acknowledged)
        self.assertEqual([], delivery.items)
        self.assertIsNone(executor._delivery_outcome)
        self.assertEqual(session.content, executor._outcome.content)
        self.assertTrue(executor.retirement_confirmed)

    async def test_delivery_ack_frees_report_capacity_while_native_cleanup_is_pending(self):
        claims, claim, acknowledged, executor, session, delivery, verified = await self.setup_execution(True)
        session.release.clear()
        task = asyncio.create_task(executor.run(acknowledged))
        try:
            await asyncio.wait_for(delivery.accepted.wait(), 1)
            await asyncio.sleep(0)
            self.assertIs(executor, claim.operation_owner)
            self.assertTrue(claims.owns_job(scope(), acknowledged.start.input.job_ref))
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            self.assertFalse(executor.retirement_confirmed)
            pending = record("key:other")
            other = replace(pending, input=replace(pending.input, job_ref="job:other"),
                            body=start_body("job:other", "key:other"))
            self.assertTrue(claims.claim_live(other, lifecycle_owner=object()).active)
        finally:
            session.release.set()
            await asyncio.wait_for(task, 1)
