"""Exercise provider-owned science without shared execution machinery."""

from provider_core.container_guard import require_container

require_container()

import unittest

from dummy_provider import science


class DummyScienceTests(unittest.TestCase):
    def test_weighted_sum_uses_provider_types_directly(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(
                scale=2,
                offset=1,
            ),
            coefficients=science.CoefficientSet(
                values=(1, 2, 3),
            ),
        )
        science.load_model(model)
        analysis = science.create_analysis(model)
        science.initialize_analysis(analysis)
        prepared = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(
                values=(2, 4, 6),
            ),
        )

        actual_result = science.calculate(
            analysis=analysis,
            state=science.CalculationState(
                prepared=prepared,
            ),
        )

        self.assertEqual(
            science.PredictionResult(
                total=57,
                count=3,
            ),
            actual_result,
        )
        science.dispose_analysis(analysis)
        science.dispose_model(model)

    def test_rejected_candidate_does_not_discard_the_analysis(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(
                scale=1,
                offset=0,
            ),
            coefficients=science.CoefficientSet(
                values=(2, 3),
            ),
        )
        science.load_model(model)
        analysis = science.create_analysis(model)
        science.initialize_analysis(analysis)

        with self.assertRaises(science.ScientificInputRejected) as rejection:
            science.prepare(
                analysis=analysis,
                candidate=science.PreparationInput(
                    values=(1,),
                ),
            )

        self.assertEqual("wrong_count", rejection.exception.code)
        corrected = science.prepare(
            analysis=analysis,
            candidate=science.PreparationInput(
                values=(1, 2),
            ),
        )
        self.assertEqual(2, corrected.value_count)
        science.dispose_analysis(analysis)
        science.dispose_model(model)

    def test_stateless_sum_needs_no_model_or_analysis(self) -> None:
        actual_result = science.sum_values(
            summation=science.SumInput(
                values=(2, 4, 6),
            )
        )

        self.assertEqual(
            science.SumResult(
                total=12,
                count=3,
            ),
            actual_result,
        )


    def test_scientific_calls_reject_other_input_types(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(2, 3)),
        )
        science.load_model(model)
        self.addCleanup(science.dispose_model, model)

        analysis = science.create_analysis(model)
        science.initialize_analysis(analysis)
        self.addCleanup(science.dispose_analysis, analysis)

        with self.assertRaises(TypeError):
            science.prepare(
                analysis=analysis,
                candidate=science.SumInput(values=(1, 2)),
            )
        with self.assertRaises(TypeError):
            science.sum_values(
                summation=science.PreparationInput(values=(1, 2)),
            )
        with self.assertRaises(TypeError):
            science.calculate(
                analysis=analysis,
                state=science.PreparationInput(values=(1, 2)),
            )

    def test_prepared_values_validate_type_before_emptiness(self) -> None:
        model = science.create_model(
            settings=science.WeightedSumSettings(scale=1, offset=0),
            coefficients=science.CoefficientSet(values=(1,)),
        )
        analysis = science.create_analysis(model)

        with self.assertRaises(TypeError):
            science.PreparedValues(analysis=analysis, values=[])
        with self.assertRaises(ValueError):
            science.PreparedValues(analysis=analysis, values=())


if __name__ == "__main__":
    unittest.main()
