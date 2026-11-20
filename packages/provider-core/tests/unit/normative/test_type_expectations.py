"""Type diagnostics retain admission semantics, field locations and failure order."""

from dataclasses import dataclass, replace
from typing import Literal
import unittest
from unittest.mock import patch

from provider_core import payload
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.payload import JsonDataclassCodec, PayloadRejected


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedNumericLiteral:
    selection: Literal[True, 2]


@dataclass(frozen=True, slots=True, kw_only=True)
class DistinctLiteralTypes:
    selection: Literal[True, "x"]


@dataclass(frozen=True, slots=True, kw_only=True)
class NumericRecord:
    number: float


class TypeExpectationTests(unittest.TestCase):
    def test_capability_type_failures_identify_the_invalid_inventory(self):
        for inventory_name in ("contract_versions", "isolation"):
            with self.subTest(inventory_name=inventory_name):
                with self.assertRaises(TypeError) as rejected:
                    replace(InProcessExecutionHost.capabilities, **{inventory_name: ()})

                self.assertIn(inventory_name, str(rejected.exception))

    def test_literal_keeps_independent_value_and_type_acceptance(self):
        codec = JsonDataclassCodec(format="test.literal.v1", value_type=MixedNumericLiteral)
        expected = MixedNumericLiteral(selection=1)

        self.assertEqual(first=expected, second=codec.decode(payload=b'{"selection":1}'))
        self.assertEqual(first=b'{"selection":1}', second=codec.encode(value=expected))

    def test_literal_type_failure_reports_full_inventory_and_path(self):
        codec = JsonDataclassCodec(format="test.literal.v1", value_type=DistinctLiteralTypes)

        with self.assertRaises(expected_exception=PayloadRejected) as rejected:
            codec.decode(payload=b'{"selection":1}')

        self.assertIs(PayloadRejected.Code.INVALID_VALUE, rejected.exception.code)
        self.assertIn(member="$.selection", container=str(rejected.exception))
        self.assertIn(member="type(value)=<class 'int'> ≠ expected_types=[<class 'bool'>, <class 'str'>]", container=str(rejected.exception))

        with self.assertRaises(expected_exception=TypeError) as rejected:
            codec.encode(value=DistinctLiteralTypes(selection=1))

        self.assertIn(member="$.selection", container=str(rejected.exception))
        self.assertIn(member="expected_types=[<class 'bool'>, <class 'str'>]", container=str(rejected.exception))

    def test_literal_value_rejection_does_not_claim_a_type_mismatch(self):
        codec = JsonDataclassCodec(format="test.literal.v1", value_type=MixedNumericLiteral)

        with self.assertRaises(expected_exception=PayloadRejected) as rejected:
            codec.decode(payload=b'{"selection":3}')

        self.assertIs(PayloadRejected.Code.INVALID_VALUE, rejected.exception.code)
        self.assertNotIn(member="≠", container=str(rejected.exception))

        with self.assertRaises(expected_exception=TypeError) as rejected:
            codec.encode(value=MixedNumericLiteral(selection=3))

        self.assertNotIn(member="≠", container=str(rejected.exception))

    def test_literal_value_rejection_precedes_type_inspection(self):
        for conversion in [payload._decode_value, payload._encode_value]:
            with self.subTest(conversion=conversion), patch.object(target=payload, attribute="type", side_effect=AssertionError("Type inspection ran before value admission"), create=True):
                with self.assertRaises(expected_exception=(PayloadRejected, TypeError)):
                    conversion(value=3, annotation=Literal[True, 2], value_path="$.selection")

    def test_float_admission_keeps_bool_separate_from_int_and_float(self):
        codec = JsonDataclassCodec(format="test.number.v1", value_type=NumericRecord)

        self.assertEqual(first=NumericRecord(number=2.0), second=codec.decode(payload=b'{"number":2}'))
        self.assertEqual(first=b'{"number":2.0}', second=codec.encode(value=NumericRecord(number=2)))

        with self.assertRaises(expected_exception=PayloadRejected) as rejected:
            codec.decode(payload=b'{"number":true}')

        self.assertIs(PayloadRejected.Code.INVALID_TYPE, rejected.exception.code)
        self.assertIn(member="type(value)=<class 'bool'> ≠ expected_types=(<class 'int'>, <class 'float'>)", container=str(rejected.exception))

        with self.assertRaises(expected_exception=TypeError):
            codec.encode(value=NumericRecord(number=True))

    def test_nonfinite_and_overflow_remain_value_failures(self):
        with self.assertRaises(expected_exception=PayloadRejected) as rejected:
            payload._decode_value(value=float("inf"), annotation=float, value_path="$.number")

        self.assertIs(PayloadRejected.Code.INVALID_TYPE, rejected.exception.code)
        self.assertNotIn(member="≠", container=str(rejected.exception))

        with self.assertRaises(expected_exception=TypeError) as rejected:
            payload._encode_value(value=float("inf"), annotation=float, value_path="$.number")

        self.assertNotIn(member="≠", container=str(rejected.exception))

        for conversion in [payload._decode_value, payload._encode_value]:
            with self.subTest(conversion=conversion), self.assertRaises(expected_exception=OverflowError):
                conversion(value=10 ** 1000, annotation=float, value_path="$.number")

    def test_exact_container_checks_still_reject_subclasses(self):
        class ListSubclass(list):
            pass

        with self.assertRaises(expected_exception=PayloadRejected):
            payload._decode_value(value=ListSubclass(), annotation=tuple[int, ...], value_path="$.mapping")

    def test_non_dataclass_class_failure_is_not_reported_as_a_type_mismatch(self):
        with self.assertRaises(expected_exception=TypeError) as rejected:
            JsonDataclassCodec(format="test.invalid.v1", value_type=object)

        self.assertIn(member="dataclass", container=str(rejected.exception))

        with self.assertRaises(expected_exception=TypeError):
            JsonDataclassCodec(format="test.invalid.v1", value_type=object())
