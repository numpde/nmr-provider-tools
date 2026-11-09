"""Exercise provider-owned types at the shared contribution boundary."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import dataclass, replace

import provider_core.contribution as contribution
from provider_core.examples.dummy_provider import science
from provider_core.examples.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
)
from provider_core.payload import (
    DataclassStateBinding,
    JsonDataclassCodec,
    PayloadRejected,
)


class _Control:
    stop_requested = False

    def __init__(self) -> None:
        self.progress = []

    def report_progress(self, event) -> None:
        self.progress.append(event)


def _weighted_binding():
    return weighted_sum_binding(
        settings=science.WeightedSumSettings(
            scale=2,
            offset=1,
        ),
        coefficients=science.CoefficientSet(
            values=(1, 2, 3),
        ),
    )


class TypedContributionTests(unittest.TestCase):
    def test_direct_science_uses_the_same_typed_objects_without_hosting(self):
        settings = science.WeightedSumSettings(scale=2, offset=1)
        coefficients = science.CoefficientSet(values=(1, 2, 3))
        model = science.create_model(
            settings=settings,
            coefficients=coefficients,
        )
        science.load_model(model)
        analysis = science.create_analysis(model)
        science.initialize_analysis(analysis)
        prepared = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(values=(2, 4, 6)),
        )

        result = science.calculate(
            analysis=analysis,
            state=science.CalculationState(prepared=prepared),
        )

        self.assertEqual(
            science.PredictionResult(total=57, count=3),
            result,
        )

    def test_provider_types_survive_payload_state_and_result_boundaries(self):
        binding = _weighted_binding()
        (prepare_call, calculate_call) = binding.calls
        model = binding.instance.construct()
        binding.instance.initialize(model)
        analysis = binding.analysis.construct(model)
        binding.analysis.initialize(analysis)
        control = _Control()

        preparation_input = prepare_call.decode_input(b'{"values":[2,4,6]}')

        self.assertIs(science.PreparationInput, type(preparation_input))
        self.assertEqual((2.0, 4.0, 6.0), preparation_input.values)

        preparation = prepare_call.invoke(
            model,
            analysis,
            preparation_input,
            {},
            control,
        )
        prepared_values = preparation.states[0].value

        self.assertIs(science.PreparationResult, type(preparation.value))
        self.assertIs(science.PreparedValues, type(prepared_values))

        calculation = calculate_call.invoke(
            model,
            analysis,
            calculate_call.decode_input(b"{}"),
            {"prepared": prepared_values},
            control,
        )

        self.assertEqual(
            science.PredictionResult(
                total=57,
                count=3,
            ),
            calculation.value,
        )
        self.assertEqual(
            {"count": 3, "total": 57.0},
            json.loads(calculate_call.encode_result(calculation.value)),
        )

    def test_call_and_implementation_descriptions_have_one_source(self):
        bindings = (
            _weighted_binding(),
            sum_binding(),
        )

        for binding in bindings:
            with self.subTest(binding_id=binding.description.binding_id):
                self.assertEqual(
                    tuple(call.description for call in binding.calls),
                    binding.description.calls,
                )

                for call in binding.calls:
                    self.assertEqual(
                        call.description.input_format,
                        call.input_codec.format,
                    )
                    self.assertEqual(
                        call.description.result_format,
                        call.result_codec.format,
                    )
                    self.assertEqual(
                        tuple(state.name for state in call.description.state_inputs),
                        call.state_binding.names,
                    )

    def test_call_constructor_derives_provider_and_shared_marker_types(self):
        (prepare_call, calculate_call) = _weighted_binding().calls
        sum_call = sum_binding().calls[0]

        self.assertIs(science.PreparationInput, prepare_call.input_codec.value_type)
        self.assertIs(contribution.NoState, prepare_call.state_binding.value_type)
        self.assertIs(science.PreparationResult, prepare_call.result_codec.value_type)
        self.assertIs(contribution.NoInput, calculate_call.input_codec.value_type)
        self.assertIs(science.CalculationState, calculate_call.state_binding.value_type)
        self.assertIs(science.PredictionResult, calculate_call.result_codec.value_type)
        self.assertIs(science.SumInput, sum_call.input_codec.value_type)
        self.assertIs(contribution.NoState, sum_call.state_binding.value_type)
        self.assertIs(science.SumResult, sum_call.result_codec.value_type)

    def test_equal_wire_shapes_keep_distinct_provider_types_and_formats(self):
        prepare_call = _weighted_binding().calls[0]
        sum_call = sum_binding().calls[0]

        preparation_input = prepare_call.decode_input(b'{"values":[1]}')
        sum_input = sum_call.decode_input(b'{"values":[1]}')

        self.assertIs(science.PreparationInput, type(preparation_input))
        self.assertIs(science.SumInput, type(sum_input))
        self.assertIsNot(type(preparation_input), type(sum_input))
        self.assertNotEqual(
            prepare_call.description.input_format,
            sum_call.description.input_format,
        )

    def test_payload_codec_rejects_invalid_wire_shapes(self):
        codec = sum_binding().calls[0].input_codec
        invalid_payloads = (
            b'{"values":[1],"values":[2]}',
            b'{"values":[true]}',
            b'{"values":[NaN]}',
            b'{"values":[1],"extra":2}',
            b"{}",
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(PayloadRejected):
                codec.decode(payload)

    def test_scientific_rejections_follow_structural_admission(self):
        binding = weighted_sum_binding(
            settings=science.WeightedSumSettings(
                scale=1,
                offset=0,
            ),
            coefficients=science.CoefficientSet(values=(2,)),
        )
        prepare_call = binding.calls[0]
        model = binding.instance.construct()
        binding.instance.initialize(model)
        analysis = binding.analysis.construct(model)
        binding.analysis.initialize(analysis)
        preparation_input = prepare_call.decode_input(b'{"values":[]}')

        self.assertIs(science.PreparationInput, type(preparation_input))

        with self.assertRaises(
            contribution.ScientificRejection
        ) as preparation_rejection:
            prepare_call.invoke(
                model,
                analysis,
                preparation_input,
                {},
                _Control(),
            )
        self.assertEqual("empty_input", preparation_rejection.exception.code)

        sum_call = sum_binding().calls[0]
        sum_input = sum_call.decode_input(b'{"values":[]}')

        with self.assertRaises(contribution.ScientificRejection) as sum_rejection:
            sum_call.invoke(
                None,
                None,
                sum_input,
                {},
                _Control(),
            )
        self.assertEqual("empty_input", sum_rejection.exception.code)

    def test_nonfinite_values_are_rejected_during_portable_construction(self):
        for value in (math.nan, math.inf, -math.inf, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                science.PreparationInput(values=(value,))

    def test_state_binding_constructs_provider_state(self):
        model = science.create_model(
            settings=science.WeightedSumSettings(
                scale=1,
                offset=0,
            ),
            coefficients=science.CoefficientSet(values=(1,)),
        )
        analysis = science.create_analysis(model)
        prepared = science.PreparedValues(
            analysis=analysis,
            values=(1,),
        )
        state_binding = DataclassStateBinding(
            value_type=science.CalculationState,
        )

        state = state_binding.bind({"prepared": prepared})

        self.assertIs(science.CalculationState, type(state))
        self.assertIs(prepared, state.prepared)

    def test_result_codec_rejects_another_provider_result_type(self):
        codec = sum_binding().calls[0].result_codec

        with self.assertRaises(TypeError):
            codec.encode(
                science.PredictionResult(
                    total=1,
                    count=1,
                )
            )

    def test_portable_records_must_be_frozen_and_slotted(self):
        @dataclass
        class MutableRecord:
            value: int

        with self.assertRaises(TypeError):
            JsonDataclassCodec(
                format="mutable-record.v1",
                value_type=MutableRecord,
            )

    def test_portable_records_reject_mutable_container_annotations(self):
        @dataclass(frozen=True, slots=True)
        class MutableInput:
            values: list[float]

        with self.assertRaises(TypeError):
            JsonDataclassCodec(
                format="mutable.v1",
                value_type=MutableInput,
            )

    def test_binding_requires_exact_provider_annotations(self):
        call = sum_binding().calls[0]

        def untyped(instance, analysis, scientific_input, state, control):
            return call.scientific_call(
                instance,
                analysis,
                scientific_input,
                state,
                control,
            )

        with self.assertRaises(TypeError):
            replace(
                call,
                scientific_call=untyped,
            )

    def test_implementation_rejects_inconsistent_instance_types(self):
        binding = _weighted_binding()
        (prepare_call, calculate_call) = binding.calls

        def wrong_instance(
            instance: None,
            analysis: science.WeightedSumAnalysis,
            candidate: science.PreparationInput,
            state: contribution.NoState,
            control: contribution.ScientificControl,
        ) -> contribution.ScientificReturn[science.PreparationResult]:
            raise AssertionError("This mismatched adapter must not run")

        mismatched_call = replace(
            prepare_call,
            scientific_call=wrong_instance,
        )
        with self.assertRaises(TypeError):
            replace(
                binding,
                calls=(mismatched_call, calculate_call),
            )

    def test_implementation_rejects_mismatched_lifecycle_hook_annotations(self):
        binding = _weighted_binding()

        def wrong_constructor() -> science.SumResult:
            return science.SumResult(total=0, count=1)

        hooks = replace(
            binding.instance,
            construct=wrong_constructor,
        )
        with self.assertRaises(TypeError):
            replace(
                binding,
                instance=hooks,
            )

    def test_binding_rejects_codec_format_mismatch(self):
        call = sum_binding().calls[0]
        wrong_codec = JsonDataclassCodec(
            format="wrong.v1",
            value_type=science.SumInput,
        )

        with self.assertRaises(ValueError):
            replace(
                call,
                input_codec=wrong_codec,
            )


if __name__ == "__main__":
    unittest.main()
