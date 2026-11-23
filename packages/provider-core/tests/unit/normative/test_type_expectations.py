"""Type diagnostics retain admission semantics, field locations and failure order."""

from dataclasses import dataclass, replace
import json
from typing import Literal
import unittest

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.payload import JsonDataclassCodec, PayloadRejected


@dataclass(frozen=True, slots=True, kw_only=True)
class NumericRecord:
    number: float


@dataclass
class StructuredRecord:
    coordinates: tuple[float, float]
    mode: Literal["adaptive"]


class TypeExpectationTests(unittest.TestCase):
    def test_capability_type_failures_identify_the_invalid_inventory(self):
        for inventory_name in ("contract_versions", "isolation"):
            with self.subTest(inventory_name=inventory_name):
                with self.assertRaises(TypeError) as rejected:
                    replace(InProcessExecutionHost.capabilities, **{inventory_name: ()})

                self.assertIn(inventory_name, str(rejected.exception))

    def test_invalid_input_retains_pydantic_field_diagnostics_and_cause(self):
        codec = JsonDataclassCodec(format="test.number.v1", value_type=NumericRecord)
        with self.assertRaises(PayloadRejected) as rejected:
            codec.decode(b'{"number":"not a number"}')
        self.assertIn("number", str(rejected.exception))
        self.assertIsNotNone(rejected.exception.__cause__)
        self.assertEqual(PayloadRejected.Code.INVALID_TYPE, rejected.exception.code)
        self.assertNotIn("not a number", str(rejected.exception))

    def test_record_rejections_preserve_sdk_categories(self):
        codec = JsonDataclassCodec(format="test.structured.v1", value_type=StructuredRecord)
        valid = {"coordinates": [1.0, 2.0], "mode": "adaptive"}
        cases = (
            ({**valid, "extra": "private spectrum"}, PayloadRejected.Code.UNEXPECTED_FIELD),
            ({"coordinates": [1.0, 2.0]}, PayloadRejected.Code.MISSING_FIELD),
            ({**valid, "coordinates": [1.0]}, PayloadRejected.Code.INVALID_LENGTH),
            ({**valid, "coordinates": [1.0, 2.0, 3.0]}, PayloadRejected.Code.INVALID_LENGTH),
            ({**valid, "mode": "private spectrum"}, PayloadRejected.Code.INVALID_VALUE),
        )
        for document, expected in cases:
            with self.subTest(document=document), self.assertRaises(PayloadRejected) as rejected:
                codec.decode(json.dumps(document).encode())
            self.assertEqual(expected, rejected.exception.code)
            self.assertNotIn("private spectrum", str(rejected.exception))

    def test_nonfinite_json_remains_rejected(self):
        codec = JsonDataclassCodec(format="test.number.v1", value_type=NumericRecord)
        for encoded in (b'{"number":NaN}', b'{"number":1e999}'):
            with self.subTest(encoded=encoded), self.assertRaises(PayloadRejected):
                codec.decode(encoded)
        with self.assertRaises(ValueError):
            codec.encode(NumericRecord(number=float("inf")))

    def test_non_dataclass_type_is_rejected_at_registration(self):
        for value_type in (object, object()):
            with self.subTest(value_type=value_type), self.assertRaises(TypeError):
                JsonDataclassCodec(format="invalid.v1", value_type=value_type)
