"""Exercise provider-owned science without shared execution machinery."""

from provider_core.container_guard import require_container

require_container()

import unittest

import mock_numpy as np

from dummy_provider import science


class DummyScienceTests(unittest.TestCase):
    def test_weighted_sum_uses_provider_types_directly(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(weights=np.array(object=[1, 2, 3], dtype=float)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        prepared_samples = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=np.array(object=[2, 4, 6], dtype=float)),
        )

        actual_prediction = science.calculate(
            analysis=analysis,
            state=science.CalculationState(prepared=prepared_samples),
        )

        self.assertEqual(science.PredictionResult(total=57, count=3), actual_prediction)

    def test_rejected_candidate_does_not_discard_the_analysis(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=[2, 3], dtype=float)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        with self.assertRaises(science.ScientificInputRejected) as rejection:
            science.prepare(
                analysis=analysis,
                candidate=science.PreparationInput(samples=np.array(object=[1], dtype=float)),
            )

        self.assertEqual("wrong_count", rejection.exception.code)

        corrected_preparation = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=np.array(object=[1, 2], dtype=float)),
        )

        self.assertEqual(2, corrected_preparation.sample_count)

    def test_stateless_sum_needs_no_model_or_analysis(self) -> None:
        actual_result = science.sum_values(summation=science.SumInput(samples=np.array(object=[2, 4, 6], dtype=float)))

        self.assertEqual(science.SumResult(total=12, count=3), actual_result)

    def test_scientific_calls_reject_other_input_types(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=[2, 3], dtype=float)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        with self.assertRaises(TypeError):
            science.prepare(
                analysis=analysis,
                candidate=science.SumInput(samples=np.array(object=[1, 2], dtype=float)),
            )

        with self.assertRaises(TypeError):
            science.sum_values(
                summation=science.PreparationInput(samples=np.array(object=[1, 2], dtype=float)),
            )

        with self.assertRaises(TypeError):
            science.calculate(
                analysis=analysis,
                state=science.PreparationInput(samples=np.array(object=[1, 2], dtype=float)),
            )

    def test_samples_reject_wrong_shape_and_nonfinite_numbers(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=[1], dtype=float)),
        )

        analysis = science.create_analysis(model=model)

        for raw_samples in ([], [[1.0]], [float("nan")], [float("inf")], [-float("inf")]):
            with self.subTest(raw_samples=raw_samples), self.assertRaises(expected_exception=ValueError):
                science.PreparedSamples(analysis=analysis, samples=np.array(object=raw_samples, dtype=float))

    def test_non_real_sample_arrays_are_rejected_before_calculation(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1.0, offset=0.0),
            coefficients=science.CoefficientSet(weights=np.array(object=[1.0], dtype=float)),
        )

        analysis = science.create_analysis(model=model)

        for raw_samples in ([True], ["1.0"], [1 + 2j]):
            with self.subTest(raw_samples=raw_samples):
                sample_array = np.array(object=raw_samples)

                with self.assertRaises(expected_exception=ValueError):
                    science.PreparedSamples(analysis=analysis, samples=sample_array)

    def test_model_and_preparation_own_copies_of_their_arrays(self) -> None:
        coefficient_weights = np.array(object=[2.0, 3.0], dtype=float)

        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=coefficient_weights),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model=model)

        analysis = science.create_analysis(model=model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis=analysis)

        request_samples = np.array(object=[4.0, 5.0], dtype=float)

        prepared_samples = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=request_samples),
        )

        coefficient_weights[0] = 99.0
        request_samples[0] = 99.0

        self.assertEqual(first=[2.0, 3.0], second=model.loaded_coefficients.tolist())
        self.assertEqual(first=[4.0, 5.0], second=prepared_samples.samples.tolist())

        prediction = science.calculate(
            analysis=analysis,
            state=science.CalculationState(prepared=prepared_samples),
        )

        self.assertEqual(first=science.PredictionResult(total=23.0, count=2), second=prediction)
        self.assertIsNone(obj=prepared_samples.samples)
        self.assertIsNone(obj=analysis.accepted_preparation)

        with self.assertRaises(expected_exception=RuntimeError):
            prepared_samples.consume(analysis=analysis)

    def test_integer_inputs_accumulate_without_integer_overflow(self) -> None:
        sample_array = np.array(object=[2 ** 63 - 1, 1])
        summation_result = science.sum_values(summation=science.SumInput(samples=sample_array))

        self.assertEqual(first=float(2 ** 63), second=summation_result.total)

        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1.0, offset=0.0),
            coefficients=science.CoefficientSet(weights=np.array(object=[1, 1])),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model=model)

        analysis = science.create_analysis(model=model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis=analysis)

        prepared_samples = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=sample_array),
        )

        prediction = science.calculate(
            analysis=analysis,
            state=science.CalculationState(prepared=prepared_samples),
        )

        self.assertEqual(first=float(2 ** 63), second=prediction.total)

    def test_nonfinite_model_settings_are_rejected_directly(self) -> None:
        for nonfinite_scalar in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(scale=nonfinite_scalar), self.assertRaises(expected_exception=ValueError):
                science.WeightedSumSettings(scale=nonfinite_scalar, offset=0.0)

            with self.subTest(offset=nonfinite_scalar), self.assertRaises(expected_exception=ValueError):
                science.WeightedSumSettings(scale=1.0, offset=nonfinite_scalar)


if __name__ == "__main__":
    unittest.main()
