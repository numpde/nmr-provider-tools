"""Largest admitted fields fit the whole live control allowance, beyond START."""

from dataclasses import replace
import unittest

from provider_core.attempt_record import ProtectedEvidence, StartPending, SuppressionReason, TerminalOperation, _encode_pending
from provider_core.attempt_request import start_body
from provider_core.attempt_response import read_start
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint,
    EvidenceDigest, SuppressedCheckpoint, TerminalSelectedCheckpoint, encode_checkpoint,
)
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import start_response


class LiveControlBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_complete_control_projection_fits_the_reserved_128_kib(self):
        job, provider, kind = "job:" + "j" * 124, "provider:" + "p" * 119, "k" * 128
        facts = replace(record().input, job_ref=job, input_byte_length=65536)
        body = start_body(job, "k" * 128)
        body += b" " * (4096 - len(body))
        largest_scope = replace(scope(), provider_ref=provider, consumer_id="\U0001f9ea" * 1024,
                                generation_id="\U0001f9ea" * 1024)
        pending = StartPending(largest_scope, kind, facts, body)
        padding = 65536 - len(_encode_pending(pending))
        pending = replace(pending, scope=replace(largest_scope, api_origin=largest_scope.api_origin + "a" * padding))
        self.assertEqual(65536, len(_encode_pending(pending)))
        self.assertEqual(4096, len(pending.body))

        admission = read_start(
            start_response(job_ref=job, provider_ref=provider, analysis_kind_ref=kind,
                           started_at="9999-12-31T23:59:59.999999Z"),
            job_ref=job, provider_ref=provider, analysis_kind_ref=kind,
        )
        acknowledged = AcknowledgedCheckpoint(pending, admission)
        evidence = ProtectedEvidence("\U0001f9ea" * 1024, b"p" * 262144)
        entered = EnteredCheckpoint(acknowledged, EvidenceDigest.from_evidence(evidence))
        selected = TerminalSelectedCheckpoint(entered, TerminalOperation.COMPLETE, "sha256:" + "f" * 64, 2097152)
        accepted = DeliveryAcceptedCheckpoint(selected)
        suppressed = SuppressedCheckpoint(entered, SuppressionReason.OBSERVATION_FAILED)
        claims = AttemptClaims.live()
        claim = claims.claim_live(pending, lifecycle_owner=object())
        for projection in (acknowledged, entered, selected, accepted, suppressed):
            with self.subTest(phase=type(projection).__name__):
                encoded = encode_checkpoint(projection)
                self.assertLessEqual(len(encoded), 131072)
                self.assertNotIn(evidence.content, encoded)
        self.assertEqual(2097152, claim.maximum_terminal_bytes)
