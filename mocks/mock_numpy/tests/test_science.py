"""Exercise the scalar finiteness behavior required by the dummy provider."""

import unittest

import mock_numpy as np


class ScalarFinitenessTests(unittest.TestCase):
    def test_finite_and_nonfinite_scalars(self):
        for (numeric_scalar, expected_finiteness) in (
            (0, True),
            (-1, True),
            (1.25, True),
            (-0.0, True),
            (float("nan"), False),
            (float("inf"), False),
            (-float("inf"), False),
        ):
            with self.subTest(numeric_scalar=numeric_scalar):
                self.assertEqual(first=expected_finiteness, second=np.isfinite(numeric_scalar))

    def test_unsupported_inputs_preserve_the_native_type_error(self):
        for unsupported_input in (None, "1.0", [1.0]):
            with self.subTest(unsupported_input=unsupported_input), self.assertRaises(expected_exception=TypeError):
                np.isfinite(unsupported_input)
