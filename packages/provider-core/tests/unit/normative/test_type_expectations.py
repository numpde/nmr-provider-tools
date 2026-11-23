"""Type diagnostics retain admission semantics, field locations and failure order."""

from dataclasses import dataclass, replace
import json
import unittest

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.payload import JsonDataclassCodec, PayloadRejected


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

    def test_invalid_input_retains_pydantic_field_diagnostics_and_cause(self):
        codec = JsonDataclassCodec(format="test.number.v1", value_type=NumericRecord)

        with self.assertRaises(PayloadRejected) as rejected:
            codec.decode(b'{"number":"not a number"}')

        self.assertIn("number", str(rejected.exception))
        self.assertIsNotNone(rejected.exception.__cause__)
        self.assertEqual(PayloadRejected.Code.INVALID_VALUE, rejected.exception.code)
        self.assertNotIn("not a number", str(rejected.exception))
        diagnostics = json.loads(rejected.exception.message)

        self.assertEqual(["number"], diagnostics[0]["loc"])
        self.assertNotIn("input", diagnostics[0])

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
