"""Check shared construction of contribution descriptions and revisions."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

import provider_core.contribution_declaration as declaration
from tests.fakes.dummy_provider import science
from tests.fakes.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
)


class ContributionDeclarationTests(unittest.TestCase):
    def test_weighted_binding_uses_shared_configuration_asset_and_revision(self):
        settings = science.WeightedSumSettings(
            scale=2,
            offset=1,
        )
        coefficients = science.CoefficientSet(
            values=(1, 2, 3),
        )

        binding = weighted_sum_binding(
            settings=settings,
            coefficients=coefficients,
        )
        configuration = declaration.json_configuration(
            settings,
            format_id="dummy.settings.v1",
        )
        coefficient_asset = declaration.json_asset(
            coefficients,
            name="coefficients",
            format_id="dummy.coefficients.v1",
            media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
        )

        self.assertEqual(configuration, binding.description.configuration)
        self.assertEqual((coefficient_asset,), binding.description.assets)
        self.assertEqual(
            declaration.binding_revision(
                implementation="dummy.weighted-sum.v2",
                configuration=configuration,
                assets=(coefficient_asset,),
            ),
            binding.description.binding_revision,
        )

    def test_stateless_binding_does_not_invent_configuration(self):
        binding = sum_binding()

        self.assertIsNone(binding.description.configuration)
        self.assertEqual(
            declaration.binding_revision(
                implementation="dummy.sum.v5",
                configuration=None,
                assets=(),
            ),
            binding.description.binding_revision,
        )

    def test_revision_changes_when_configuration_changes(self):
        first = declaration.json_configuration(
            science.WeightedSumSettings(
                scale=1,
                offset=0,
            ),
            format_id="dummy.settings.v1",
        )
        second = declaration.json_configuration(
            science.WeightedSumSettings(
                scale=2,
                offset=0,
            ),
            format_id="dummy.settings.v1",
        )

        self.assertNotEqual(
            declaration.binding_revision(
                implementation="dummy.weighted-sum.v2",
                configuration=first,
                assets=(),
            ),
            declaration.binding_revision(
                implementation="dummy.weighted-sum.v2",
                configuration=second,
                assets=(),
            ),
        )

    def test_absent_and_explicit_empty_configuration_are_distinct(self):
        @dataclass(frozen=True, slots=True, kw_only=True)
        class EmptyConfiguration:
            pass

        explicit_empty = declaration.json_configuration(
            EmptyConfiguration(),
            format_id="test.empty.v1",
        )

        self.assertNotEqual(
            declaration.binding_revision(
                implementation="test.implementation.v1",
                configuration=None,
                assets=(),
            ),
            declaration.binding_revision(
                implementation="test.implementation.v1",
                configuration=explicit_empty,
                assets=(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
