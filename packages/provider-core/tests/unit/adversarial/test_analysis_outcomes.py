"""Reject raw identifiers and mutable result bytes before publication."""

from dataclasses import FrozenInstanceError
from unittest.mock import patch
import unittest

from provider_core.analysis import AnalysisFailure, AnalysisSuccess
from provider_core.attempt_record import ProtectedEvidence
from provider_core.contribution_errors import ErrorCode
from provider_core.payload import EncodedPayload
from tests.fakes.outcomes import CountingCodec, FailureCode


class AnalysisOutcomeRefusalTests(unittest.TestCase):
    def setUp(self):
        self.evidence = ProtectedEvidence("fixture.evidence.v1", b"evidence")

    def test_failure_refuses_raw_strings_including_an_enum_wire_value(self):
        for code in ("invalid_input", FailureCode.INVALID_INPUT.value, 1):
            with self.subTest(code=code), self.assertRaises(TypeError):
                AnalysisFailure(code=code, message="Supply observations.", evidence=self.evidence)

    def test_failure_refuses_a_declared_code_outside_the_public_wire_profile(self):
        malformed = ErrorCode("Malformed", {"INVALID": "not/a/public/code"})

        with self.assertRaises(ValueError):
            AnalysisFailure(code=malformed.INVALID, message="Supply observations.", evidence=self.evidence)

    def test_success_refuses_an_independent_schema_and_content(self):
        with self.assertRaises(TypeError):
            AnalysisSuccess(result_schema_id="fixture.result.v1", content=b"result", evidence=self.evidence)

        for result in ("fixture.result.v1", b"result", ("fixture.result.v1", b"result")):
            with self.subTest(result=result), self.assertRaises(TypeError):
                AnalysisSuccess(result=result, evidence=self.evidence)

        with self.assertRaises(TypeError):
            EncodedPayload(format="fixture.result.v1", content=b"result")

    def test_invalid_codec_format_never_starts_encoding(self):
        codec = CountingCodec()
        codec.current_format = "format with spaces"

        with self.assertRaises(ValueError):
            EncodedPayload(codec=codec, value=bytearray(b"result"))

        self.assertEqual(codec.encodes, 0)
        self.assertEqual(codec.format_reads, 1)

    def test_codec_failure_is_preserved_and_mutable_output_is_rejected(self):
        codec = CountingCodec()
        failure = OSError("encoding failed")

        with patch.object(codec, "encode", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                EncodedPayload(codec=codec, value=bytearray(b"result"))

        self.assertIs(caught.exception, failure)

        with patch.object(codec, "encode", return_value=bytearray(b"mutable")):
            with self.assertRaises(TypeError):
                EncodedPayload(codec=codec, value=bytearray(b"result"))

    def test_admitted_result_cannot_be_relabelled_or_replaced(self):
        result = EncodedPayload(codec=CountingCodec(), value=bytearray(b"result"))
        outcome = AnalysisSuccess(result=result, evidence=self.evidence)

        with self.assertRaises(FrozenInstanceError):
            result.format = "different.v2"

        with self.assertRaises(FrozenInstanceError):
            result.content = b"replacement"

        with self.assertRaises(FrozenInstanceError):
            outcome.result = None
