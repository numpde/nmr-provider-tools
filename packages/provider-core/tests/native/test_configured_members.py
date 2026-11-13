"""Check named call ownership independently of equal portable metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
import unittest

import provider_core.contribution as contribution
from tests.fakes.dummy_provider import science
from tests.fakes.dummy_provider.binding import sum_binding, weighted_sum_binding


@dataclass(frozen=True, slots=True, kw_only=True)
class MismatchedBinding(contribution.ImplementationBinding[None, None]):
    run: contribution.CallBinding[
        science.SumInput, contribution.NoState, science.PreparationResult,
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedBinding(contribution.ImplementationBinding[
    science.WeightedSumModel, science.WeightedSumAnalysis,
]):
    run: contribution.CallBinding[
        science.SumInput, contribution.NoState, science.SumResult,
    ]


class UnusedControl:
    stop_requested = False

    def report_progress(self, event: contribution.ProgressEvent) -> None:
        raise AssertionError("The plain sum must not report progress")


class ConfiguredMemberTests(unittest.TestCase):
    def test_reference_projects_the_actual_named_member(self):
        binding = sum_binding()
        reference = contribution.ref(binding.run)

        self.assertEqual(binding.description.binding_id, reference.binding_id)
        self.assertEqual(binding.description.binding_revision, reference.binding_revision)
        self.assertEqual("run", reference.name)
        self.assertEqual(reference, binding.ref(binding.run))

    def test_replacing_a_member_does_not_copy_its_ownership(self):
        binding = sum_binding()
        unbound = replace(binding.run)

        with self.assertRaisesRegex(ValueError, "before a configured binding owns it"):
            contribution.ref(unbound)
        self.assertEqual("run", contribution.ref(binding.run).name)

    def test_sibling_rejects_a_member_even_when_metadata_are_equal(self):
        first, sibling = sum_binding(), sum_binding()
        self.assertEqual(first.description, sibling.description)
        self.assertEqual(contribution.ref(first.run), contribution.ref(sibling.run))

        with self.assertRaisesRegex(ValueError, "another configured binding"):
            sibling.ref(first.run)

    def test_reusing_a_declaration_creates_independent_owned_members(self):
        original = sum_binding()
        declaration = replace(original.run)
        first = replace(original, run=declaration)
        second = replace(original, run=declaration)

        self.assertIs(first.identity, second.identity)
        self.assertIsNot(declaration, first.run)
        self.assertIsNot(declaration, second.run)
        self.assertIsNot(first.run, second.run)
        self.assertEqual(contribution.ref(first.run), contribution.ref(second.run))
        self.assertEqual(contribution.ref(original.run), original.ref(original.run))
        with self.assertRaisesRegex(ValueError, "before a configured binding owns it"):
            contribution.ref(declaration)
        with self.assertRaisesRegex(ValueError, "another configured binding"):
            first.ref(second.run)

    def test_declared_call_cannot_be_absent(self):
        binding = sum_binding()

        with self.assertRaisesRegex(TypeError, "must contain a CallBinding"):
            replace(binding, run=None)

    def test_member_annotation_must_match_its_actual_codec_types(self):
        binding = sum_binding()

        with self.assertRaisesRegex(TypeError, "codecs differ from its declared types"):
            MismatchedBinding(
                identity=binding.identity,
                instance=None,
                analysis=None,
                resident_resources=(),
                isolation=binding.isolation,
                run=binding.run,
            )

    def test_plain_function_coexists_with_real_instance_and_analysis_hooks(self):
        weighted = weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )
        plain = sum_binding()
        binding = MixedBinding(
            identity=plain.identity,
            instance=weighted.instance,
            analysis=weighted.analysis,
            resident_resources=(),
            isolation=weighted.isolation,
            run=plain.run,
        )
        model = binding.instance.construct()
        analysis = binding.analysis.construct(model)
        try:
            # The unary sum has no dependency on loaded coefficients or an
            # initialized analysis, although its implementation owns both hooks.
            self.assertIsNone(model.loaded_coefficients)
            self.assertFalse(analysis.is_open)
            returned = binding.run.invoke(
                model, analysis, science.SumInput(values=(1, 2, 3)),
                {}, UnusedControl(),
            )
            self.assertEqual(science.SumResult(total=6, count=3), returned.value)
            self.assertEqual((), returned.states)
            self.assertIsNone(model.loaded_coefficients)
            self.assertFalse(analysis.is_open)
        finally:
            binding.analysis.dispose(analysis)
            binding.instance.dispose(model)


if __name__ == "__main__":
    unittest.main()
