"""Outcome identities come from declared codes and the codec that encoded bytes."""

from dataclasses import dataclass
from hashlib import sha256
import unittest

from provider_core.analysis import AnalysisFailure, AnalysisSuccess
from provider_core.attempt_record import ProtectedEvidence
from provider_core.payload import EncodedPayload, JsonDataclassCodec
from tests.fakes.outcomes import CountingCodec, FailureCode


@dataclass
class Report:
    count: int


class AnalysisOutcomeTests(unittest.TestCase):
    def test_failure_retains_the_producers_declared_code(self):
        evidence = ProtectedEvidence("fixture.failure.v1", b"private evidence")
        outcome = AnalysisFailure(code=FailureCode.INVALID_INPUT, message="Supply observations.", evidence=evidence)

        self.assertIs(outcome.code, FailureCode.INVALID_INPUT)
        self.assertEqual(outcome.code.value, "invalid_input")
        self.assertIs(outcome.evidence, evidence)

    def test_success_binds_the_json_schema_to_the_encoded_report(self):
        codec = JsonDataclassCodec(format="fixture.report.v1", value_type=Report)
        result = EncodedPayload(codec=codec, value=Report(count=3))
        evidence = ProtectedEvidence("fixture.result_digest.v1", sha256(result.content).hexdigest().encode())
        outcome = AnalysisSuccess(result=result, evidence=evidence)

        self.assertEqual(outcome.result.format, "fixture.report.v1")
        self.assertEqual(codec.decode(outcome.result.content), Report(count=3))
        self.assertEqual(sha256(outcome.result.content).hexdigest().encode(), outcome.evidence.content)

    def test_binary_result_is_frozen_before_the_source_and_codec_change(self):
        codec = CountingCodec()
        source = bytearray(b"\x00\xff")
        result = EncodedPayload(codec=codec, value=source)
        outcome = AnalysisSuccess(result=result, evidence=ProtectedEvidence("fixture.binary.evidence.v1", b"evidence"))
        source.append(1)
        codec.current_format = "fixture.changed.v2"

        self.assertIs(outcome.result, result)
        self.assertEqual(outcome.result.format, "fixture.binary.v1")
        self.assertEqual(outcome.result.content, b"\x00\xff")
        self.assertEqual(codec.format_reads, 1)
        self.assertEqual(codec.encodes, 1)
