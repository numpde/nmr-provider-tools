import json
import unittest
from dataclasses import dataclass, replace

from provider_core.contribution import ScientificRejection
from provider_core.examples.dummy_provider import science
from provider_core.examples.dummy_provider.binding import sum_binding, weighted_binding
from provider_core.payload import DataclassStateBinding, JsonDataclassCodec, PayloadRejected


class Control:
    stop_requested = False

    def __init__(self):
        self.progress = []

    def report_progress(self, event):
        self.progress.append(event)


class TypedBoundaryTests(unittest.TestCase):
    def test_provider_types_survive_shared_json_and_call_boundary(self):
        binding = weighted_binding(science.Settings(scale=2, offset=1), (1, 2, 3))
        prepare, calculate = binding.calls
        model = binding.instance.construct()
        binding.instance.initialize(model)
        analysis = binding.analysis.construct(model)
        binding.analysis.initialize(analysis)
        control = Control()

        candidate = prepare.decode_input(b'{"values":[2,4,6]}')
        self.assertIs(type(candidate), science.ValuesInput)
        self.assertEqual((2.0, 4.0, 6.0), candidate.values)
        prepared_return = prepare.invoke(model, analysis, candidate, {}, control)
        self.assertIs(type(prepared_return.value), science.PreparationResult)
        prepared = prepared_return.states[0].value

        request = calculate.decode_input(b'{}')
        self.assertIs(type(request), science.EmptyInput)
        result = calculate.invoke(model, analysis, request, {"prepared": prepared}, control)
        self.assertIs(type(result.value), science.PredictionResult)
        self.assertEqual(science.PredictionResult(total=57.0, count=3), result.value)
        self.assertEqual(
            {"count": 3, "total": 57.0},
            json.loads(calculate.encode_result(result.value)),
        )

    def test_payload_codec_is_strict_and_structural(self):
        codec = sum_binding().calls[0].input_codec
        for payload in (
            b'{"values":[1],"values":[2]}',
            b'{"values":[true]}',
            b'{"values":[NaN]}',
            b'{"values":[1],"extra":2}',
            b'{}',
        ):
            with self.subTest(payload=payload), self.assertRaises(PayloadRejected):
                codec.decode(payload)

    def test_scientific_rejection_is_after_typed_admission(self):
        binding = weighted_binding(science.Settings(scale=1, offset=0), (2,))
        prepare = binding.calls[0]
        model = binding.instance.construct()
        binding.instance.initialize(model)
        analysis = binding.analysis.construct(model)
        binding.analysis.initialize(analysis)
        candidate = prepare.input_codec.decode(b'{"values":[]}')
        self.assertIs(type(candidate), science.ValuesInput)
        with self.assertRaises(ScientificRejection) as caught:
            prepare.invoke(model, analysis, candidate, {}, Control())
        self.assertEqual("empty_input", caught.exception.code)

    def test_state_binding_constructs_provider_struct(self):
        prepared = science.PreparedInput(
            analysis=science.AnalysisState(
                model=science.create_model(
                    science.Settings(scale=1, offset=0),
                    (1,),
                )
            ),
            values=(1.0,),
        )
        binding = DataclassStateBinding(value_type=science.CalculationState)
        state = binding.bind({"prepared": prepared})
        self.assertIs(type(state), science.CalculationState)
        self.assertIs(prepared, state.prepared)

    def test_result_codec_rejects_wrong_provider_type(self):
        codec = sum_binding().calls[0].result_codec
        with self.assertRaises(TypeError):
            codec.encode(science.PredictionResult(total=1.0, count=1))

    def test_portable_structs_must_be_frozen_and_slotted(self):
        @dataclass
        class MutableRecord:
            value: int

        with self.assertRaises(TypeError):
            JsonDataclassCodec(
                format="mutable-record.v1",
                value_type=MutableRecord,
            )

    def test_portable_structs_reject_mutable_container_annotations(self):
        @dataclass(frozen=True, slots=True)
        class MutableInput:
            values: list[float]

        with self.assertRaises(TypeError):
            JsonDataclassCodec(
                format="mutable.v1",
                value_type=MutableInput,
            )

    def test_binding_requires_exact_provider_type_annotations(self):
        call = sum_binding().calls[0]

        def untyped(instance, analysis, candidate, state, control):
            return call.scientific_call(
                instance,
                analysis,
                candidate,
                state,
                control,
            )

        with self.assertRaises(TypeError):
            replace(call, scientific_call=untyped)

    def test_binding_rejects_codec_format_mismatch(self):
        call = sum_binding().calls[0]
        wrong = JsonDataclassCodec(format="wrong.v1", value_type=science.ValuesInput)
        with self.assertRaises(ValueError):
            replace(call, input_codec=wrong)


if __name__ == "__main__":
    unittest.main()
