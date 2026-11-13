"""Check shared construction of contribution descriptions and revisions."""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace

from provider_core.contribution import CallRef, ConfiguredIdentity
import provider_core.contribution_declaration as declaration
from tests.fakes.dummy_provider import science
from tests.fakes.dummy_provider.binding import (
    sum_binding,
    weighted_sum_binding,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EmptyConfiguration:
    pass


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
            ConfiguredIdentity(
                binding_id="dummy.weighted-sum",
                implementation="dummy.weighted-sum.v2",
                contract_version=1,
                configuration=configuration,
                assets=(coefficient_asset,),
            ).revision,
            binding.description.binding_revision,
        )

    def test_stateless_binding_does_not_invent_configuration(self):
        binding = sum_binding()

        self.assertIsNone(binding.description.configuration)
        self.assertEqual(
            ConfiguredIdentity(
                binding_id="dummy.sum",
                implementation="dummy.sum.v5",
                contract_version=1,
                configuration=None,
                assets=(),
            ).revision,
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
            ConfiguredIdentity(
                binding_id="dummy.weighted-sum",
                implementation="dummy.weighted-sum.v2",
                contract_version=1,
                configuration=first,
                assets=(),
            ).revision,
            ConfiguredIdentity(
                binding_id="dummy.weighted-sum",
                implementation="dummy.weighted-sum.v2",
                contract_version=1,
                configuration=second,
                assets=(),
            ).revision,
        )

    def test_absent_and_explicit_empty_configuration_are_distinct(self):
        explicit_empty = declaration.json_configuration(
            EmptyConfiguration(),
            format_id="test.empty.v1",
        )

        self.assertNotEqual(
            ConfiguredIdentity(
                binding_id="test.implementation",
                implementation="test.implementation.v1",
                contract_version=1,
                configuration=None,
                assets=(),
            ).revision,
            ConfiguredIdentity(
                binding_id="test.implementation",
                implementation="test.implementation.v1",
                contract_version=1,
                configuration=explicit_empty,
                assets=(),
            ).revision,
        )

    def test_revision_covers_complete_configuration_and_asset_descriptions(self):
        configuration = declaration.json_configuration(
            science.WeightedSumSettings(scale=2, offset=1),
            format_id="dummy.settings.v1",
        )
        asset = declaration.json_asset(
            science.CoefficientSet(values=(1, 2, 3)),
            name="coefficients",
            format_id="dummy.coefficients.v1",
            media_type="application/json",
        )
        identity = ConfiguredIdentity(
            binding_id="dummy.weighted-sum",
            implementation="dummy.weighted-sum.v2",
            contract_version=1,
            configuration=configuration,
            assets=(asset,),
        )
        variants = {
            "configuration format": replace(
                identity,
                configuration=replace(configuration, format="dummy.settings.v2"),
            ),
            "asset name": replace(identity, assets=(replace(asset, name="weights"),)),
            "asset media type": replace(
                identity, assets=(replace(asset, media_type="application/octet-stream"),),
            ),
            "asset byte count": replace(
                identity, assets=(replace(asset, size_bytes=asset.size_bytes + 1),),
            ),
            "contract version": replace(identity, contract_version=2),
        }
        for label, changed in variants.items():
            with self.subTest(changed=label):
                self.assertNotEqual(identity.revision, changed.revision)
        self.assertRegex(identity.revision, r"\Aconfigured-v2:sha256:[0-9a-f]{64}\Z")
        self.assertRegex(configuration.digest, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertRegex(asset.digest, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(configuration.digest, variants["configuration format"].configuration.digest)
        for label in ("asset name", "asset media type", "asset byte count"):
            self.assertEqual(asset.digest, variants[label].assets[0].digest)

    def test_legacy_call_revision_remains_distinct_from_configured_revision(self):
        current = sum_binding().identity.revision
        legacy = "sha256:" + current.removeprefix("configured-v2:sha256:")
        old_reference = CallRef("dummy.sum", legacy, "run")
        new_reference = CallRef("dummy.sum", current, "run")

        self.assertEqual(legacy, old_reference.binding_revision)
        self.assertEqual(current, new_reference.binding_revision)
        self.assertNotEqual(old_reference, new_reference)


if __name__ == "__main__":
    unittest.main()
