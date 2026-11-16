"""Exercise provider-owned science without shared execution machinery."""

from provider_core.container_guard import require_container

require_container()

import unittest

from dummy_provider import science


class DummyScienceTests(unittest.TestCase):
    def test_weighted_sum_uses_provider_types_directly(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(weights=(1, 2, 3)),
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
            coefficients=science.CoefficientSet(weights=(2, 3)),
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
            coefficients=science.CoefficientSet(weights=(2, 3)),
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

    def test_samples_reject_wrong_container_and_nonfinite_numbers(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(weights=(1,)),
        )

        analysis = science.create_analysis(model=model)

        with self.assertRaises(expected_exception=TypeError):
            science.PreparedSamples(analysis=analysis, samples=[])

        for invalid_samples in ((), (float("nan"),), (float("inf"),), (-float("inf"),)):
            with self.subTest(invalid_samples=invalid_samples), self.assertRaises(expected_exception=ValueError):
                science.PreparedSamples(analysis=analysis, samples=invalid_samples)

        for invalid_samples in ((True,), ("1",), (1 + 2j,)):
            with self.subTest(invalid_samples=invalid_samples), self.assertRaises(expected_exception=TypeError):
                science.PreparedSamples(analysis=analysis, samples=invalid_samples)


if __name__ == "__main__":
    unittest.main()
