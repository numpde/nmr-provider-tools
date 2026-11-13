"""Check the scientific sequence independently of execution and transport."""

from __future__ import annotations

import unittest

from provider_core.contribution_errors import ScientificRejection
from spectrum_provider import science


class SpectrumScienceTests(unittest.TestCase):
    def test_correction_then_detection_preserves_positions_and_reports_peaks(self) -> None:
        source = science.Spectrum(
            positions=(0, 1, 2, 3, 4),
            intensities=(1, 4, 2, 5, 1),
        )
        corrected = science.subtract_baseline(source, science.BaselineSettings(baseline=1))
        report = science.detect_peaks(corrected, science.PeakSettings(minimum_height=2))

        self.assertEqual(source.positions, corrected.positions)
        self.assertEqual((0, 3, 1, 4, 0), corrected.intensities)
        self.assertEqual((1, 4, 2, 5, 1), source.intensities)
        self.assertEqual(
            science.PeakReport(
                peaks=(science.Peak(position=1, height=3), science.Peak(position=3, height=4)),
            ),
            report,
        )

    def test_correction_preserves_negative_intensities(self) -> None:
        corrected = science.subtract_baseline(
            science.Spectrum(positions=(0,), intensities=(1,)),
            science.BaselineSettings(baseline=2),
        )
        self.assertEqual((-1,), corrected.intensities)

    def test_detection_excludes_endpoints_plateaus_and_below_threshold_maxima(self) -> None:
        report = science.detect_peaks(
            science.Spectrum(
                positions=(0, 1, 2, 3, 4, 5, 6, 7, 8),
                intensities=(9, 0, 4, 4, 0, 1, 0, 2, 9),
            ),
            science.PeakSettings(minimum_height=2),
        )
        self.assertEqual((), report.peaks)

    def test_detection_includes_a_strict_maximum_equal_to_the_threshold(self) -> None:
        report = science.detect_peaks(
            science.Spectrum(positions=(0, 1, 2), intensities=(0, 2, 0)),
            science.PeakSettings(minimum_height=2),
        )
        self.assertEqual((science.Peak(position=1, height=2),), report.peaks)

    def test_peak_detection_rejects_too_few_samples(self) -> None:
        with self.assertRaises(ScientificRejection) as rejected:
            science.detect_peaks(
                science.Spectrum(positions=(0, 1), intensities=(0, 2)),
                science.PeakSettings(minimum_height=2),
            )
        self.assertEqual("too_few_samples", rejected.exception.code)
        self.assertIn("at least three samples", rejected.exception.message)

    def test_spectrum_rejects_empty_mismatched_and_unordered_samples(self) -> None:
        for positions, intensities in (
            ((), ()),
            ((0, 1), (1,)),
            ((0, 0), (1, 2)),
            ((1, 0), (1, 2)),
        ):
            with self.subTest(positions=positions, intensities=intensities):
                with self.assertRaises(ValueError):
                    science.Spectrum(positions=positions, intensities=intensities)

    def test_nonfinite_and_boolean_measurements_and_settings_are_rejected(self) -> None:
        for value in (float("inf"), float("-inf"), float("nan"), True):
            expected_error = TypeError if type(value) is bool else ValueError
            for constructor in (
                lambda: science.Spectrum(positions=(value,), intensities=(1,)),
                lambda: science.Spectrum(positions=(0,), intensities=(value,)),
                lambda: science.BaselineSettings(baseline=value),
                lambda: science.PeakSettings(minimum_height=value),
            ):
                with self.subTest(value=value, constructor=constructor):
                    with self.assertRaises(expected_error):
                        constructor()

    def test_baseline_subtraction_rejects_numerical_overflow(self) -> None:
        with self.assertRaisesRegex(ArithmeticError, "Baseline subtraction.*nonfinite"):
            science.subtract_baseline(
                science.Spectrum(positions=(0,), intensities=(1e308,)),
                science.BaselineSettings(baseline=-1e308),
            )


if __name__ == "__main__":
    unittest.main()
