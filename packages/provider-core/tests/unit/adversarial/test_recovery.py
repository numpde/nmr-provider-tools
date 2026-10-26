"""Missing relay ownership and interrupted writes never authorize new science."""

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from provider_core.recovery import AttemptRecovery, RecoveryFailed
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, observation
from tests.fakes.checkpoint_execution import acknowledged_store
from tests.unit.normative.test_recovery import Relay, selected


class CompactRecoveryRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_attempt_without_relay_ownership_is_retained_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            try:
                checkpoint, _ = await selected(store, acknowledged)
                relay, transport = Relay(), Transport(reply(observation()))
                with self.assertRaises(RecoveryFailed):
                    await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                          delivery=relay).recover(checkpoint)
                self.assertEqual(await store.inventory(), (checkpoint,))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0].method, "GET")
                self.assertEqual(relay.items, [])
                claim.release()
            finally:
                await store.close()

    async def test_mismatched_relay_choice_does_not_clear_or_overwrite_selected_identity(self):
        for field, value in (("job_ref", "job:other"), ("operation", "complete"),
                             ("body_sha256", "sha256:" + "9" * 64), ("body_bytes", 1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                try:
                    checkpoint, item = await selected(store, acknowledged)
                    relay = Relay({**item.acceptance(), "body_bytes": len(item.body), field: value})
                    transport = Transport()
                    with self.assertRaises(RecoveryFailed):
                        await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                              delivery=relay).recover(checkpoint)
                    self.assertEqual(await store.inventory(), (checkpoint,))
                    self.assertEqual((transport.calls, relay.items), ([], []))
                    claim.release()
                finally:
                    await store.close()

    async def test_unbound_or_unreadable_api_reply_preserves_control(self):
        for body in (b"{}", observation(execution_attempt_ref=ATTEMPT_REF[:-1] + "b")):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                try:
                    checkpoint = AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission)
                    relay, transport = Relay(), Transport(reply(body))
                    with self.assertRaises(RecoveryFailed):
                        await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                              delivery=relay).recover(checkpoint)
                    self.assertEqual(await store.inventory(), (checkpoint,))
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(relay.items, [])
                    claim.release()
                finally:
                    await store.close()

    async def test_stale_checkpoint_sends_nothing_and_keeps_actual_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            try:
                checkpoint = AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission)
                stale = replace(checkpoint, admission=replace(checkpoint.admission, replayed=True))
                transport, relay = Transport(), Relay()
                with self.assertRaises(RecoveryFailed):
                    await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                          delivery=relay).recover(stale)
                self.assertEqual(await store.inventory(), (checkpoint,))
                self.assertEqual((transport.calls, relay.items), ([], []))
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_clear_settles_the_original_write_without_second_get(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            checkpoint = AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission)
            transport, relay = Transport(reply(observation(state="failed"))), Relay()
            recovery = AttemptRecovery(api(transport, store.scope.api_origin), store, claim, delivery=relay)
            entered, release = threading.Event(), threading.Event()
            original = store._database.clear_observed_after_cleanup
            def delayed(*arguments):
                entered.set()
                release.wait(3)
                return original(*arguments)
            task = None
            try:
                with patch.object(store._database, "clear_observed_after_cleanup", side_effect=delayed) as clear:
                    task = asyncio.create_task(recovery.recover(checkpoint))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    await recovery.settle()
                    self.assertEqual(clear.call_count, 1)
                self.assertEqual(await store.inventory(), ())
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(relay.items, [])
                claim.release()
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_operation is not None:
                    await store.settle(owner=store.pending_owner)
                await store.close()

    async def test_atomic_claim_inventory_keeps_existing_owner_and_reserves_no_partial_aliases(self):
        claims = AttemptClaims()
        first = AcknowledgedCheckpoint(record(), admission())
        second = AcknowledgedCheckpoint(record("second"), admission())
        existing = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim_records((first, second))
        claims.claim_start(first.start).release()
        claims.claim_start(second.start).release()
        existing.require(scope(), ATTEMPT_REF)
        existing.release()
        with self.assertRaises(OwnershipRefused):
            claims.claim_records((first, second))
        claims.claim_start(first.start).release()
        claims.claim_start(second.start).release()
        claims.claim(scope(), ATTEMPT_REF).release()
