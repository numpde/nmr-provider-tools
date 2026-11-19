"""Offering construction admits API fields before announcement aggregation."""

import os
import unittest

from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering


class HelloOfferingValidationTests(unittest.TestCase):
    def _invalid_offerings(self):
        return (
            ("PRIVATE invalid", "d", ValueError),
            ("nmr_predict", "PRIVATE\udfff", ValueError),
            ("nmr_predict", "PRIVATE\0value", ValueError),
            ("nmr_predict", "", ValueError),
            ("nmr_predict", "x" * 1025, ValueError),
            ("nmr_predict", [], TypeError),
        )

    def test_offering_fields_are_admitted_at_construction_before_aggregation(self):
        for (kind, description, error_type) in self._invalid_offerings():
            with self.subTest(kind_valid=kind == "nmr_predict"), self.assertRaises(error_type):
                AnalysisOffering(kind, description)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_offering_rejections_omit_input(self):
        for (kind, description, error_type) in self._invalid_offerings():
            with self.subTest(kind_valid=kind == "nmr_predict"), self.assertRaises(error_type) as caught:
                AnalysisOffering(kind, description)

            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_offering_length_refusal_supplies_counts(self):
        with self.assertRaises(ValueError) as caught:
            AnalysisOffering("nmr_predict", "PRIVATE" + "x" * 1330)

        self.assertIn("1337", str(caught.exception))
        self.assertIn("1024", str(caught.exception))

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_offering_length_refusal_omits_description_content(self):
        with self.assertRaises(ValueError) as caught:
            AnalysisOffering("nmr_predict", "PRIVATE" + "x" * 1330)

        self.assertNotIn("PRIVATE", str(caught.exception))
