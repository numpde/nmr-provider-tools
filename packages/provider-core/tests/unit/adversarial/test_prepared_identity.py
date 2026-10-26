"""No fresh science dispatch may omit or replace its retained candidate identity."""

import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock

from provider_core.attempt_record import MAX_EVIDENCE_BYTES, ProtectedEvidence, acknowledge_start, mark_execution_entered
from provider_core.execution import AttemptExecutor, ExecutionFailed
from provider_core.producer_checkpoint import (
    MAX_CHECKPOINT_BYTES, AcknowledgedCheckpoint, EnteredCheckpoint, EvidenceDigest,
    decode_checkpoint, encode_checkpoint,
)
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import observation, progress_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response


class PreparedIdentityRefusalTests(unittest.TestCase):
    def test_protected_evidence_refuses_empty_mutable_and_oversized_content(self):
        for content in (b"", bytearray(b"mutable"), b"x" * (MAX_EVIDENCE_BYTES + 1)):
            with self.subTest(content_type=type(content).__name__), self.assertRaises(ValueError):
                ProtectedEvidence("fixture.input.v1", content)

    def test_entry_requires_explicit_protected_identity(self):
        acknowledged = acknowledge_start(record(), admission())
        with self.assertRaises(TypeError):
            mark_execution_entered(acknowledged)
        for value in (None, b"unbound identity", object()):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                mark_execution_entered(acknowledged, value)

    def test_identity_cannot_be_attached_before_execution_entry(self):
        acknowledged = acknowledge_start(record(), admission())
        with self.assertRaises(ValueError):
            replace(acknowledged, prepared_identity=ProtectedEvidence("fixture.input.v1", b"identity"))

    def test_checkpoint_entry_requires_its_actual_prepared_digest(self):
        acknowledged = AcknowledgedCheckpoint(record(), admission())
        for value in (None, b"unbound identity", ProtectedEvidence("fixture.input.v1", b"identity")):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                EnteredCheckpoint(acknowledged, value)

    def test_checkpoint_restore_keeps_its_actual_metadata_byte_limit(self):
        raw = encode_checkpoint(AcknowledgedCheckpoint(record(), admission()))
        with self.assertRaises(ValueError):
            decode_checkpoint(raw + b" " * (MAX_CHECKPOINT_BYTES + 1 - len(raw)), scope=scope())

    def test_entry_digest_is_required_and_checked_even_when_state_is_forged(self):
        entered = EnteredCheckpoint(AcknowledgedCheckpoint(record(), admission()),
            EvidenceDigest.from_evidence(ProtectedEvidence("fixture.input.v1", b"identity")))
        document = json.loads(encode_checkpoint(entered))
        for change in (
            {"prepared": None},
            {"prepared": {"schema_id": "fixture.input.v1", "sha256": "private-canary", "byte_length": 8}},
            {"prepared": document["prepared"] | {"content": "private-canary"}},
            {"prepared": document["prepared"] | {"byte_length": 0}},
            {"kind": "acknowledged"},
        ):
            with self.subTest(changed_fields=tuple(change)), self.assertRaises(ValueError) as caught:
                decode_checkpoint(json.dumps(document | change).encode(), scope=scope())
            self.assertNotIn("private-canary", str(caught.exception))
        del document["prepared"]
        with self.assertRaises(ValueError):
            decode_checkpoint(json.dumps(document).encode(), scope=scope())

class UnidentifiedPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unidentified_handle_retires_before_running_progress_or_execution(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.preparation = object()
            try:
                executor = AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                )
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIsInstance(failed.exception.cause, TypeError)
                self.assertEqual(["prepare", "retire"], [event[0] for event in session.events])
                self.assertEqual((AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),), await store.inventory())
                self.assertEqual(4, len(transport.calls))
                claim.release()
            finally:
                await store.close()
