"""Check indexed cosine matching without hosting or analysis orchestration."""

import unittest

from provider_core.contribution_errors import ScientificRejection
from reference_provider import science


class ReferenceScienceTests(unittest.TestCase):
    def index(self):
        return science.build_index(science.ReferenceSet(entries=(
            science.Reference(label="pattern-a", values=(3, 4, 0)),
            science.Reference(label="pattern-b", values=(0, 0, 1)),
        )))

    def test_index_retains_normalized_sparse_postings_and_matches_repeated_queries(self):
        index = self.index()
        self.assertEqual((((0, 0.6),), ((0, 0.8),), ((1, 1.0),)), index.postings)
        settings = science.MatchSettings(minimum_score=0.5)
        for values, label in (((3, 4, 0), "pattern-a"), ((0, 0, 2), "pattern-b")):
            with self.subTest(values=values):
                report = science.match(index, settings, science.VectorInput(values=values))
                self.assertEqual(label, report.label)
                self.assertAlmostEqual(1.0, report.score)

    def test_equal_scores_choose_the_smallest_label_independent_of_asset_order(self):
        references = science.ReferenceSet(entries=(
            science.Reference(label="z", values=(1, 0)),
            science.Reference(label="a", values=(1, 0)),
        ))
        report = science.match(
            science.build_index(references), science.MatchSettings(minimum_score=0),
            science.VectorInput(values=(1, 0)),
        )
        self.assertEqual("a", report.label)

    def test_query_rejections_distinguish_dimensions_zero_overlap_and_threshold(self):
        index = science.build_index(science.ReferenceSet(entries=(
            science.Reference(label="axis", values=(1, 0)),
        )))
        for values, minimum, code in (
            ((1,), 0, "wrong_dimensions"),
            ((0, 0), 0, "zero_vector"),
            ((0, 1), 0, "no_overlap"),
            ((1, 1), 0.9, "below_threshold"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ScientificRejection) as rejected:
                    science.match(index, science.MatchSettings(minimum_score=minimum), science.VectorInput(values=values))
                self.assertEqual(code, rejected.exception.code)

    def test_finite_extreme_values_normalize_without_overflow_or_underflow(self):
        index = self.index()
        for values in ((3e307, 4e307, 0), (3e-307, 4e-307, 0)):
            report = science.match(index, science.MatchSettings(minimum_score=0.5), science.VectorInput(values=values))
            self.assertEqual("pattern-a", report.label)
            self.assertAlmostEqual(1, report.score)

    def test_negative_nonfinite_boolean_and_empty_vectors_are_rejected(self):
        for values, error in (
            ((-1,), ValueError), ((float("nan"),), ValueError),
            ((float("inf"),), ValueError), ((True,), TypeError), ((), ValueError),
        ):
            with self.subTest(values=values):
                with self.assertRaises(error):
                    science.VectorInput(values=values)

    def test_invalid_reference_library_and_score_configuration_are_rejected(self):
        for entries in (
            (),
            (science.Reference(label="same", values=(1,)), science.Reference(label="same", values=(2,))),
            (science.Reference(label="a", values=(1,)), science.Reference(label="b", values=(1, 2))),
        ):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    science.ReferenceSet(entries=entries)
        with self.assertRaises(ValueError):
            science.Reference(label="zero", values=(0, 0))
        for minimum in (-0.1, 1.1, float("inf")):
            with self.assertRaises(ValueError):
                science.MatchSettings(minimum_score=minimum)


if __name__ == "__main__":
    unittest.main()
