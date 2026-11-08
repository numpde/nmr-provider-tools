"""Exact local terminal ACK, bounded capacity and original lifecycle ownership.

A recording relay port controls local admission only. These tests make no claim
about subsequent publication to the Jobs API.
"""

import asyncio
import json
import unittest

from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, TerminalSelectedCheckpoint
from tests.fakes.checkpoint_execution import CausalDelivery
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer
from tests.integration.chat.adversarial.test_model_reply_permission import (
    HeldRetirementSession, InterpretedSession, ReplyRun,
)


def model_reply():
    return json.dumps({"choices": [{
        "message": call("complete", "prepare", {}), "finish_reason": "tool_calls",
    }]}).encode()


class DeliveryLifecycleInterleavings(unittest.IsolatedAsyncioTestCase):
    async def test_local_ack_frees_terminal_bytes_but_not_original_claim(self):
        limits = LiveAttemptLimits(
            maximum_attempts=2, maximum_control_bytes=262144,
            maximum_pending_terminal_bytes=16384,
        )
        claims = AttemptClaims.live(limits=limits)
        delivery = CausalDelivery()
        async with (TlsPeer("success", response_body=model_reply()) as original_peer,
                    TlsPeer("success", response_body=model_reply()) as successor_peer):
            original = ReplyRun(
                original_peer, claims, "job:handoff-held", HeldRetirementSession,
                delivery=delivery, maximum_terminal_bytes=16384,
            )
            successor = ReplyRun(
                successor_peer, claims, "job:handoff-independent", InterpretedSession,
                maximum_terminal_bytes=16384,
            )
            original.session.science.release.set()
            successor.session.science.release.set()
            driver = asyncio.create_task(original.controller.run(original.pending))
            try:
                await asyncio.wait_for(delivery.wait_for_offers(1), 6)
                await asyncio.wait_for(original.session.retiring.wait(), 6)
                selected, = delivery.offers
                self.assertIs(type(original.controller.operation._checkpoint), TerminalSelectedCheckpoint)
                self.assertEqual((1, 1), (
                    original.session.science.preparations, original.session.science.executions,
                ))
                self.assertFalse(delivery.accepted.is_set())
                self.assertFalse(original.controller.shutdown_ready)
                self.assertEqual((original.controller,), claims.live_owners())

                # The claim registry can hold two entries, but its one 16 KiB
                # terminal reservation is occupied until the matching relay ACK.
                with self.assertRaises(LiveCapacityFull):
                    claims.claim_live(
                        successor.pending, lifecycle_owner=successor.controller,
                        maximum_terminal_bytes=16384,
                    )
                self.assertEqual(0, successor.start_count)

                delivery.answer("accepted")
                with self.assertRaises(ControllerFailed):
                    await asyncio.wait_for(asyncio.shield(driver), 6)
                self.assertTrue(original.controller.operation.delivery_accepted)
                self.assertEqual([selected], delivery.items)
                self.assertEqual(0, original.controller._claim._pending_terminal_bytes)
                self.assertFalse(original.session.retirement_completed)
                self.assertFalse(original.controller.shutdown_ready)
                self.assertEqual((original.controller,), claims.live_owners())

                # B now owns the released terminal allowance, finishes its own
                # science, and releases its own claim while A still owns cleanup.
                accepted = await asyncio.wait_for(
                    successor.controller.run(successor.pending), 6)
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertEqual((1, 1), (
                    successor.session.science.preparations, successor.session.science.executions,
                ))
                self.assertEqual(1, successor.start_count)
                self.assertEqual(1, len(successor.controller.delivery.items))
                self.assertEqual((original.controller,), claims.live_owners())
                self.assertFalse(original.session.retirement_completed)

                original.session.release_retirement.set()
                await asyncio.wait_for(original.controller.settle(), 6)
                self.assertTrue(original.session.retirement_completed)
                self.assertTrue(original.controller.shutdown_ready)
                self.assertEqual((), claims.live_owners())
                self.assertEqual(1, len(original_peer.requests))
                self.assertEqual(1, len(successor_peer.requests))
            finally:
                delivery.release_for_teardown()
                original.session.release_retirement.set()
                original.session.science.release.set()
                if not driver.done():
                    original.controller.request_stop()
                await asyncio.wait_for(
                    asyncio.gather(driver, return_exceptions=True), 8)

    async def test_retryable_local_refusal_resends_only_the_original_terminal(self):
        for first_response in ("full", "retryable"):
            with self.subTest(first_response=first_response):
                claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
                delivery = CausalDelivery()
                async with TlsPeer("success", response_body=model_reply()) as peer:
                    run = ReplyRun(
                        peer, claims, "job:retry", InterpretedSession,
                        delivery=delivery,
                    )
                    run.session.science.release.set()
                    driver = asyncio.create_task(run.controller.run(run.pending))
                    try:
                        await asyncio.wait_for(delivery.wait_for_offers(1), 6)
                        first = delivery.offers[0]
                        self.assertFalse(delivery.accepted.is_set())
                        delivery.answer(first_response)
                        await asyncio.wait_for(delivery.wait_for_offers(2), 6)
                        second = delivery.offers[1]
                        self.assertIs(first, second, "Retry replaced the immutable selected terminal")
                        self.assertEqual((first.key, first.body_sha256, first.body),
                                         (second.key, second.body_sha256, second.body))
                        self.assertEqual([], delivery.items)
                        self.assertEqual((run.controller,), claims.live_owners())
                        self.assertFalse(run.controller.shutdown_ready)
                        delivery.answer("accepted")
                        result = await asyncio.wait_for(asyncio.shield(driver), 6)
                        self.assertIs(type(result), DeliveryAcceptedCheckpoint)
                        self.assertEqual(1, len(delivery.items))
                        self.assertIs(delivery.items[0], first)
                        self.assertEqual(2, len(delivery.offers))
                        self.assertEqual((1, 1, 1), (
                            run.start_count, run.session.science.preparations,
                            run.session.science.executions,
                        ))
                        self.assertTrue(run.controller.shutdown_ready)
                        self.assertEqual((), claims.live_owners())
                    finally:
                        delivery.release_for_teardown()
                        if not driver.done():
                            run.controller.request_stop()
                        await asyncio.wait_for(
                            asyncio.gather(driver, return_exceptions=True), 8)

    async def test_lost_or_invalid_ack_does_not_release_or_replay_terminal(self):
        for disposition in ("lost", "rejected"):
            with self.subTest(disposition=disposition):
                claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=1))
                delivery = CausalDelivery()
                async with TlsPeer("success", response_body=model_reply()) as peer:
                    run = ReplyRun(
                        peer, claims, "job:unconfirmed", InterpretedSession,
                        delivery=delivery,
                    )
                    run.session.science.release.set()
                    driver = asyncio.create_task(run.controller.run(run.pending))
                    try:
                        await asyncio.wait_for(delivery.wait_for_offers(1), 6)
                        original, = delivery.offers
                        self.assertIs(type(run.controller.operation._checkpoint), TerminalSelectedCheckpoint)
                        delivery.answer(disposition)
                        with self.assertRaises(ControllerFailed) as caught:
                            await asyncio.wait_for(asyncio.shield(driver), 6)
                        self.assertIs(caught.exception, run.controller.last_failure)
                        self.assertEqual(1, len(delivery.offers))
                        self.assertFalse(delivery.accepted.is_set())
                        self.assertIs(run.controller.operation._delivery_outcome, original)
                        self.assertEqual([original] if disposition == "lost" else [], delivery.items)
                        self.assertTrue(run.controller.work_settled)
                        self.assertFalse(run.controller.shutdown_ready)
                        self.assertEqual((run.controller,), claims.live_owners())
                        self.assertEqual((1, 1, 1), (
                            run.start_count, run.session.science.preparations,
                            run.session.science.executions,
                        ))
                        with self.assertRaises(ControllerFailed):
                            await asyncio.wait_for(run.controller.settle(), 6)
                        self.assertEqual(1, len(delivery.offers))
                        self.assertEqual((run.controller,), claims.live_owners())
                    finally:
                        delivery.release_for_teardown()
                        if not driver.done():
                            run.controller.request_stop()
                        await asyncio.wait_for(
                            asyncio.gather(driver, return_exceptions=True), 8)
