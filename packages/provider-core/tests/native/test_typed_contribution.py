"""Exercise provider-owned types at the shared contribution boundary."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import dataclass, replace

import provider_core.contribution as contribution
from provider_core.payload import (
    DataclassStateBinding,
    JsonDataclassCodec,
    PayloadRejected,
)
from provider_core.state_slots import SlotStateBinding, StateKind
from tests.fakes.dummy_provider import science
from tests.fakes.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
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
        prepare_call, calculate_call = binding.prepare, binding.calculate
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
            {"candidate": prepared_values},
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

        for binding, calls in ((bindings[0], (bindings[0].prepare, bindings[0].calculate)), (bindings[1], (bindings[1].run,))):
            with self.subTest(binding_id=binding.description.binding_id):
                self.assertIs(type(binding.description.calls), tuple)
                self.assertEqual(
                    [call.description for call in calls],
                    list(binding.description.calls),
                )

                for call in calls:
                    self.assertIs(type(call.state_binding.names), tuple)
                    self.assertEqual(
                        call.description.input_format,
                        call.input_codec.format,
                    )
                    self.assertEqual(
                        call.description.result_format,
                        call.result_codec.format,
                    )
                    self.assertEqual(
                        [state.name for state in call.description.state_inputs],
                        list(call.state_binding.names),
                    )

    def test_call_constructor_derives_provider_and_shared_marker_types(self):
        binding = _weighted_binding()
        prepare_call, calculate_call = binding.prepare, binding.calculate
        sum_call = sum_binding().run

        self.assertIs(science.PreparationInput, prepare_call.input_codec.value_type)
        self.assertIs(contribution.NoState, prepare_call.state_binding.value_type)
        self.assertIs(science.PreparationResult, prepare_call.result_codec.value_type)
        self.assertIs(contribution.NoInput, calculate_call.input_codec.value_type)
        self.assertEqual(
            "provider-core.no-input.v1",
            calculate_call.description.input_format,
        )
        self.assertIs(science.CalculationState, calculate_call.state_binding.value_type)
        self.assertIs(science.PredictionResult, calculate_call.result_codec.value_type)
        self.assertIs(science.SumInput, sum_call.input_codec.value_type)
        self.assertIs(contribution.NoState, sum_call.state_binding.value_type)
        self.assertIs(science.SumResult, sum_call.result_codec.value_type)

    def test_equal_wire_shapes_keep_distinct_provider_types_and_formats(self):
        prepare_call = _weighted_binding().prepare
        sum_call = sum_binding().run

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
        codec = sum_binding().run.input_codec
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
        prepare_call = binding.prepare
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

        sum_call = sum_binding().run
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
        codec = sum_binding().run.result_codec

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

    def test_context_parameter_name_has_no_semantic_role(self):
        call = sum_binding().run

        def arbitrary_name(
            values: contribution.CallContext[None, None, science.SumInput, contribution.NoState],
        ) -> contribution.ScientificReturn[science.SumResult]:
            self.assertIsNone(values.control)
            return call.scientific_call(values)

        adapted = replace(call, scientific_call=arbitrary_name)
        returned = adapted.invoke(None, None, science.SumInput(values=(2, 3)), {}, _Control())
        self.assertEqual(science.SumResult(total=5, count=2), returned.value)

    def test_binding_requires_one_typed_context_parameter(self):
        call = sum_binding().run

        def untyped(values):
            return call.scientific_call(values)

        def keyword_only(
            *, values: contribution.CallContext[None, None, science.SumInput, contribution.NoState],
        ) -> contribution.ScientificReturn[science.SumResult]:
            return call.scientific_call(values)

        for callback in (untyped, keyword_only):
            with self.subTest(callback=callback), self.assertRaises(TypeError):
                replace(call, scientific_call=callback)

    def test_no_input_call_rejects_a_provider_specific_empty_format(self):
        calculate_call = _weighted_binding().calculate

        with self.assertRaises(ValueError):
            contribution.CallBinding.from_json_dataclasses(
                name=calculate_call.description.name,
                input_type=contribution.NoInput,
                state_type=science.CalculationState,
                result_type=science.PredictionResult,
                input_format="dummy.empty.v1",
                result_format=calculate_call.description.result_format,
                state_binding=calculate_call.state_binding,
                state_outputs=calculate_call.description.state_outputs,
                resources=calculate_call.description.resources,
                concurrency=calculate_call.description.concurrency,
                interruption=calculate_call.description.interruption,
                progress=calculate_call.description.progress,
                scientific_call=calculate_call.scientific_call,
            )

    def test_undeclared_control_capability_is_unavailable(self):
        call = sum_binding().run

        def controlled(
            values: contribution.CallContext[None, None, science.SumInput, contribution.NoState],
        ) -> contribution.ScientificReturn[science.SumResult]:
            values.require_control()
            return call.scientific_call(values)

        adapted = replace(call, scientific_call=controlled)
        with self.assertRaises(RuntimeError):
            adapted.invoke(None, None, science.SumInput(values=(1,)), {}, _Control())

    def test_implementation_rejects_inconsistent_instance_types(self):
        binding = _weighted_binding()
        prepare_call, calculate_call = binding.prepare, binding.calculate

        def wrong_instance(
            values: contribution.CallContext[
                science.SumResult, science.WeightedSumAnalysis,
                science.PreparationInput, contribution.NoState,
            ],
        ) -> contribution.ScientificReturn[science.PreparationResult]:
            raise AssertionError("This mismatched adapter must not run")

        mismatched_call = replace(
            prepare_call,
            scientific_call=wrong_instance,
        )
        with self.assertRaises(TypeError):
            replace(
                binding,
                prepare=mismatched_call,
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

    def test_output_slot_inventory_cannot_be_mutated_after_construction(self):
        call = _weighted_binding().prepare
        with self.assertRaisesRegex(TypeError, "immutable tuple"):
            replace(call, _output_slots=list(call._output_slots))

    def test_state_input_kind_and_consumption_cannot_diverge(self):
        call = _weighted_binding().calculate
        parameter = call.description.state_inputs[0]
        variants = (
            replace(parameter, kind="wrong-kind"),
            replace(parameter, use=contribution.StateUse.BORROW),
        )
        for parameter in variants:
            with self.subTest(parameter=parameter), self.assertRaisesRegex(ValueError, "kind/use"):
                replace(call, description=replace(call.description, state_inputs=(parameter,)))

    def test_one_state_kind_cannot_mean_different_python_types(self):
        binding = _weighted_binding()
        wrong_input = StateKind(
            binding.prepared_input.kind.name, science.SumResult,
        ).input(name=binding.prepared_input.name, use=contribution.StateUse.CONSUME)
        wrong_state = SlotStateBinding(
            value_type=science.CalculationState,
            inputs=(wrong_input,),
            assemble=lambda values: science.CalculationState(prepared=wrong_input.read(values)),
        )
        wrong_call = replace(binding.calculate, state_binding=wrong_state)
        with self.assertRaisesRegex(TypeError, "different Python types"):
            replace(binding, calculate=wrong_call)

    def test_binding_rejects_codec_format_mismatch(self):
        call = sum_binding().run
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
