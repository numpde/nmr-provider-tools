"""Exercise provider-owned science without shared execution machinery."""

from provider_core.container_guard import require_container

require_container()

import unittest

import numpy as np

from dummy_provider import science


class DummyScienceTests(unittest.TestCase):
    def test_weighted_sum_uses_provider_types_directly(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(weights=np.array(object=(1, 2, 3), dtype=np.float64)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        prepared_samples = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=(2, 4, 6)),
        )

        actual_prediction = science.calculate(
            analysis=analysis,
            state=science.CalculationState(prepared=prepared_samples),
        )

        self.assertEqual(science.PredictionResult(total=57, count=3), actual_prediction)

    def test_rejected_candidate_does_not_discard_the_analysis(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=(2, 3), dtype=np.float64)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        with self.assertRaises(science.ScientificInputRejected) as rejection:
            science.prepare(
                analysis=analysis,
                candidate=science.PreparationInput(samples=(1,)),
            )

        self.assertEqual("wrong_count", rejection.exception.code)

        corrected_preparation = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(samples=(1, 2)),
        )

        self.assertEqual(2, corrected_preparation.sample_count)

    def test_stateless_sum_needs_no_model_or_analysis(self) -> None:
        actual_result = science.sum_values(summation=science.SumInput(samples=(2, 4, 6)))

        self.assertEqual(science.SumResult(total=12, count=3), actual_result)

    def test_scientific_calls_reject_other_input_types(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=(2, 3), dtype=np.float64)),
        )

        self.addCleanup(science.dispose_model, model)
        science.load_model(model)

        analysis = science.create_analysis(model)

        self.addCleanup(science.dispose_analysis, analysis)
        science.initialize_analysis(analysis)

        with self.assertRaises(TypeError):
            science.prepare(
                analysis=analysis,
                candidate=science.SumInput(samples=(1, 2)),
            )

        with self.assertRaises(TypeError):
            science.sum_values(
                summation=science.PreparationInput(samples=(1, 2)),
            )

        with self.assertRaises(TypeError):
            science.calculate(
                analysis=analysis,
                state=science.PreparationInput(samples=(1, 2)),
            )

    def test_vectors_reject_wrong_shape_dtype_nonfinite_and_empty_samples(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=np.array(object=(1,), dtype=np.float64)),
        )

        analysis = science.create_analysis(model=model)

        for invalid_samples in (
            np.array(object=1.0),
            np.array(object=((1.0,),)),
            np.array(object=(True,)),
            np.array(object=("1",)),
            np.array(object=(1 + 2j,)),
            np.array(object=(np.nan,)),
            np.array(object=(np.inf,)),
            np.array(object=(-np.inf,)),
            np.array(object=(), dtype=np.float64),
        ):
            with self.subTest(invalid_samples=invalid_samples), self.assertRaises(expected_exception=ValueError):
                science.PreparedSamples(analysis=analysis, samples=invalid_samples)

    def test_array_conversion_uses_numpy_numeric_promotion(self) -> None:
        coefficient_weights = np.array(object=(True, 2.0))
        coefficients = science.CoefficientSet(weights=coefficient_weights)

        np.testing.assert_array_equal(actual=coefficients.weights, desired=(1.0, 2.0))
        self.assertEqual(first=np.dtype(np.float64), second=coefficients.weights.dtype)

    def test_retained_vectors_own_their_storage(self) -> None:
        original_weights = np.array(object=(2.0, 3.0))
        coefficients = science.CoefficientSet(weights=original_weights)

        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=coefficients,
        )

        analysis = science.create_analysis(model=model)
        original_samples = np.array(object=(4.0, 5.0))
        prepared_samples = science.PreparedSamples(analysis=analysis, samples=original_samples)


        original_weights[:] = 99
        original_samples[:] = 99

        np.testing.assert_array_equal(actual=coefficients.weights, desired=(2.0, 3.0))
        np.testing.assert_array_equal(actual=prepared_samples.samples, desired=(4.0, 5.0))
        self.assertFalse(expr=coefficients.weights.flags.writeable)
        self.assertFalse(expr=prepared_samples.samples.flags.writeable)

        analysis.accepted_preparation = prepared_samples
        consumed_samples = prepared_samples.consume(analysis=analysis)

        np.testing.assert_array_equal(actual=consumed_samples, desired=(4.0, 5.0))
        self.assertIsNone(obj=prepared_samples.samples)
        self.assertIsNone(obj=analysis.accepted_preparation)

        with self.assertRaises(expected_exception=RuntimeError):
            prepared_samples.consume(analysis=analysis)


if __name__ == "__main__":
    unittest.main()
