"""Compact recovery observes existing control; it never reconstructs science."""

import tempfile
import unittest
from dataclasses import replace

from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EvidenceDigest
from provider_core.recovery import AttemptRecovery, ObservedAttempt
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempts import observation
from tests.fakes.checkpoint_execution import RecordingDelivery, acknowledged_store
from tests.fakes.delivery import outcome


class Relay(RecordingDelivery):
    def __init__(self, row=None):
        super().__init__()
        self.row, self.reads = row, []

    async def status(self, reference):
        self.reads.append(reference)
        return self.row


async def selected(store, acknowledged):
    item = replace(outcome(provider=store.scope.provider_ref, suffix="a"), job_ref=acknowledged.start.input.job_ref)
    checkpoint = await store.select_terminal(AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission), item)
    return checkpoint, item


class CompactRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_positive_pending_or_held_relay_identity_clears_control_without_api_or_science(self):
        for state in ("pending", "held"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                try:
                    checkpoint, item = await selected(store, acknowledged)
                    relay = Relay({**item.acceptance(), "body_bytes": len(item.body), "state": state})
                    transport = Transport()
                    result = await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                                   delivery=relay).recover(checkpoint)
                    self.assertIs(type(result), DeliveryAcceptedCheckpoint)
                    self.assertEqual(result.selection, checkpoint)
                    self.assertEqual(await store.inventory(), ())
                    self.assertEqual(transport.calls, [])
                    self.assertEqual(relay.reads, [acknowledged.admission.execution_attempt_ref])
                    self.assertEqual(relay.items, [])
                    claim.release()
                finally:
                    await store.close()

    async def test_durable_acceptance_clears_without_republishing_or_querying_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            try:
                checkpoint, item = await selected(store, acknowledged)
                accepted = await store.accept_delivery(checkpoint, item.acceptance())
                relay, transport = Relay(), Transport()
                result = await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                               delivery=relay).recover(accepted)
                self.assertEqual(result, accepted)
                self.assertEqual(await store.inventory(), ())
                self.assertEqual((relay.reads, relay.items, transport.calls), ([], [], []))
                claim.release()
            finally:
                await store.close()

    async def test_terminal_api_observation_closes_entry_without_claiming_candidate_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            try:
                entry = await store.mark_entered(AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),
                                                EvidenceDigest("candidate.v1", "sha256:" + "b" * 64, 8))
                transport, relay = Transport(reply(observation(state="failed"))), Relay()
                result = await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                               delivery=relay).recover(entry)
                self.assertIs(type(result), ObservedAttempt)
                self.assertEqual(result.record, entry)
                self.assertEqual(result.observation.state, "failed")
                self.assertEqual(await store.inventory(), ())
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0].method, "GET")
                self.assertEqual(relay.items, [])
                claim.release()
            finally:
                await store.close()

    async def test_terminal_start_replay_clears_only_its_control_and_never_sends(self):
        from pathlib import Path
        from provider_core.checkpoint_store import CheckpointStore, CheckpointLimits
        from tests.fakes.attempt_store import admission, record, scope

        with tempfile.TemporaryDirectory() as directory:
            pending = record()
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
            await store.open()
            try:
                await store.retain_start(pending)
                terminal = await store.acknowledge_start(pending, admission(state="failed", replayed=True))
                claim = AttemptClaims().claim_record(terminal)
                transport, relay = Transport(), Relay()
                self.assertEqual(await AttemptRecovery(api(transport, store.scope.api_origin), store, claim,
                                                       delivery=relay).recover(terminal), terminal)
                self.assertEqual(await store.inventory(), ())
                self.assertEqual((transport.calls, relay.items), ([], []))
                claim.release()
            finally:
                await store.close()
