"""Expose scientific input records without a parallel language-model schema.

Schema assertions cover the public carrier contract. Admission assertions retain
the stricter runtime rules that JSON Schema alone cannot express.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from typing import Literal
import unittest

from provider_core.conversation import ToolFinished, ToolRejected
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import observe_cleanup
from provider_core.input_tool import ScientificInputToolDefinition
from provider_core.interpreter import Interpreter
from provider_core.payload import JsonDataclassCodec, PayloadRejected
from provider_core.scientific import Contribution, operation

from tests.fakes.conversation import call, endpoint


class Family(str, Enum):
    HF = "hf"
    CHF = "chf"


@dataclass(frozen=True, slots=True, kw_only=True)
class Peak:
    integral: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Spectrum:
    peaks: tuple[Peak, ...]
    pair: tuple[int, str]
    annotation: str | None
    family: Family = Family.HF
    label: Literal["source", "corrected"] = "source"
    count: int = 7


@dataclass(frozen=True, slots=True, kw_only=True)
class Node:
    value: int
    next: Node | None = None


class Spectra(Contribution):
    @operation()
    def prepare(self, *, spectrum: Spectrum) -> Peak:
        raise AssertionError("Advertising or decoding a tool must not run science")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.codec = JsonDataclassCodec(format="spectrum.v1", value_type=Spectrum)

    def test_nested_dataclass_objects_are_closed(self):
        schema = self.codec.json_schema()

        self.assertIs(expr1=False, expr2=schema["additionalProperties"])
        peak = schema["$defs"]["Peak"]

        self.assertIs(expr1=False, expr2=peak["additionalProperties"])
        self.assertEqual(first=["integral"], second=peak["required"])

    def test_defaults_and_required_nullable_fields_remain_distinct(self):
        schema = self.codec.json_schema()

        self.assertEqual(first={"peaks", "pair", "annotation"}, second=set(schema["required"]))
        self.assertEqual(first=7, second=schema["properties"]["count"]["default"])
        alternatives = schema["properties"]["annotation"]["anyOf"]

        self.assertIn(member={"type": "null"}, container=alternatives)
        decoded = self.codec.decode(payload=b'{"peaks":[],"pair":[1,"a"],"annotation":null}')

        self.assertEqual(first=7, second=decoded.count)
        self.assertIsNone(obj=decoded.annotation)

    def test_fixed_tuple_retains_position_and_length(self):
        pair = self.codec.json_schema()["properties"]["pair"]

        self.assertEqual(first=[{"type": "integer"}, {"type": "string"}], second=pair["prefixItems"])
        self.assertEqual(first=2, second=pair["minItems"])
        self.assertEqual(first=2, second=pair["maxItems"])

    def test_variadic_tuple_uses_its_nested_record_definition(self):
        schema = self.codec.json_schema()
        peaks = schema["properties"]["peaks"]

        self.assertEqual(first="array", second=peaks["type"])
        self.assertEqual(first={"$ref": "#/$defs/Peak"}, second=peaks["items"])
        self.assertIn(member="Peak", container=schema["$defs"])

    def test_enum_and_literal_choices_are_preserved(self):
        schema = self.codec.json_schema()

        self.assertEqual(first=["hf", "chf"], second=schema["$defs"]["Family"]["enum"])
        self.assertEqual(first=["source", "corrected"], second=schema["properties"]["label"]["enum"])

    def test_recursive_record_keeps_root_relative_references(self):
        schema = JsonDataclassCodec(format="node.v1", value_type=Node).json_schema()

        self.assertEqual(first="#/$defs/Node", second=schema["$ref"])
        node = schema["$defs"]["Node"]

        self.assertIs(expr1=False, expr2=node["additionalProperties"])
        self.assertIn(member={"$ref": "#/$defs/Node"}, container=node["properties"]["next"]["anyOf"])

    def test_exported_schema_documents_do_not_share_mutable_state(self):
        first = self.codec.json_schema()

        first["$defs"]["Peak"]["properties"].clear()
        first["required"].clear()
        second = self.codec.json_schema()

        self.assertIn(member="integral", container=second["$defs"]["Peak"]["properties"])
        self.assertIn(member="peaks", container=second["required"])

    def test_schema_generation_does_not_change_scientific_construction(self):
        self.codec.json_schema()
        peak = Peak(integral="provider code can construct its own Python object")

        self.assertEqual(first=str, second=type(peak.integral))

        with self.assertRaises(expected_exception=TypeError):
            JsonDataclassCodec(format="peak.v1", value_type=Peak).encode(value=peak)

    def test_runtime_rejects_lexical_and_unknown_field_cases_schema_cannot_admit_for_it(self):
        codec = JsonDataclassCodec(format="peak.v1", value_type=Peak)

        codec.json_schema()

        for payload in (
            b'{"integral":1.0}',
            b'{"integral":true}',
            b'{"integral":"1"}',
            b'{"integral":1,"integral":2}',
            b'{"integral":1,"extra":0}',
            b'{"integral":NaN}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(expected_exception=PayloadRejected):
                    codec.decode(payload=payload)


class ScientificInputToolDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.execution_host = InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        self.addAsyncCleanup(self.retire)
        self.hosted_contribution = self.execution_host.adopt(contribution=Spectra(), requirements=frozenset())
        self.call_binding = self.hosted_contribution.call(method=self.hosted_contribution.contribution.prepare)
        self.tool_definition = ScientificInputToolDefinition(call_binding=self.call_binding, description="Submit source-faithful spectrum data")

    async def retire(self):
        await observe_cleanup(control=self.execution_host.retire())

    def test_definition_uses_selected_call_and_direct_record_parameters(self):
        function = self.tool_definition.definition["function"]

        self.assertEqual(first="prepare", second=function["name"])
        self.assertEqual(first=self.call_binding.input_codec.json_schema(), second=function["parameters"])
        self.assertNotIn(member="value", container=function["parameters"]["properties"])
        self.assertNotIn(member="strict", container=function)

    def test_decoding_constructs_scientific_input_without_executing_science(self):
        candidate = self.tool_definition.decode(
            name=self.tool_definition.name,
            arguments={"peaks": [{"integral": 3}], "pair": [2, "a"], "annotation": None},
        )

        self.assertEqual(first=Spectrum, second=type(candidate))
        self.assertEqual(first=(Peak(integral=3),), second=candidate.peaks)

    def test_wrong_name_is_a_correctable_tool_rejection(self):
        with self.assertRaises(expected_exception=ToolRejected):
            self.tool_definition.decode(name="another_method", arguments={})

    def test_structural_failure_keeps_original_codec_rejection(self):
        with self.assertRaises(expected_exception=PayloadRejected) as raised:
            self.tool_definition.decode(name=self.tool_definition.name, arguments={"value": {}})

        self.assertEqual(first="unexpected_field", second=raised.exception.code)

    def test_custom_codec_does_not_acquire_guessed_json_semantics(self):
        original = self.call_binding.input_codec

        class CustomCodec:
            format = original.format
            value_type = original.value_type

            def encode(self, value):
                return original.encode(value=value)

            def decode(self, payload):
                return original.decode(payload=payload)

        custom = replace(self.call_binding, input_codec=CustomCodec())

        with self.assertRaisesRegex(expected_exception=TypeError, expected_regex="CustomCodec"):
            ScientificInputToolDefinition(call_binding=custom, description="Cannot infer this codec's schema")

    async def test_interpreter_can_inspect_generated_constraints_before_submitting(self):
        carrier = endpoint(
            "schema-reader",
            call("inspect", "describe_input_constraints", {"tool": self.tool_definition.name, "field": "annotation"}),
            call("submit", self.tool_definition.name, {"peaks": [], "pair": [1, "a"], "annotation": None}),
        )

        interpreter = Interpreter(
            endpoint_factories=(lambda **settings: carrier,),
            timeout_seconds=5,
            context_bytes=32768,
            secrets=(),
        )

        self.addAsyncCleanup(interpreter.retire)
        accepted = []

        async def dispatch(name, arguments, *, not_after):
            accepted.append(self.tool_definition.decode(name=name, arguments=arguments))

            return ToolFinished(value="Validated")

        await interpreter.run(messages=[], tools=[self.tool_definition.definition], dispatch=dispatch)
        self.assertEqual(first=1, second=len(accepted))
        self.assertEqual(first=Spectrum, second=type(accepted[0]))

        constraint_reply = next(
            message for message in carrier.complete.call_args_list[1].args[0]
            if message["role"] == "tool" and message["tool_call_id"] == "inspect"
        )

        constraint = json.loads(s=constraint_reply["content"])["value"]

        self.assertTrue(expr=constraint["required"])
        self.assertIn(member={"type": "null"}, container=constraint["schema"]["anyOf"])
