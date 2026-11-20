"""Exercise the array behavior required by the dummy provider."""

import math
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
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


class ArrayContractTests(unittest.TestCase):
    def test_installed_package_imports_without_other_distributions(self):
        # Copy only this installed package; the child cannot see provider-core
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(pathlib.Path(np.__file__).parent, pathlib.Path(directory) / "mock_numpy")

            subprocess.run(
                [
                    sys.executable,  # Use the installed test interpreter
                    "-I",  # Ignore ambient Python paths
                    "-S",  # Exclude every installed distribution
                    "-B",  # Keep the isolated package free of bytecode
                    "-c",  # Import only the copied wheel package and standard library
                    "import sys; sys.path.insert(0, sys.argv[1]); import mock_numpy",  # Eager imports include the bundled helper
                    directory,  # Expose only mock_numpy to the child
                ],
                check=True,
            )

    def test_vector_metadata_and_dtype_inference(self):
        for (array_input, expected_kind) in (
            ([True, False], "b"),
            ([1, 2], "i"),
            ([1, 2.5], "f"),
            ([True, 2.5], "f"),
            ([1 + 2j], "c"),
            (["1", "2"], "U"),
        ):
            with self.subTest(array_input=array_input):
                numerical_array = np.array(object=array_input)

                self.assertEqual(first=(len(array_input),), second=numerical_array.shape)
                self.assertEqual(first=1, second=numerical_array.ndim)
                self.assertEqual(first=len(array_input), second=numerical_array.size)
                self.assertEqual(first=expected_kind, second=numerical_array.dtype.kind)

    def test_empty_and_nested_arrays_preserve_shape(self):
        for (array_input, expected_shape, expected_size) in (
            ([], (0,), 0),
            ([[], []], (2, 0), 0),
            ([[1, 2], [3, 4]], (2, 2), 4),
        ):
            with self.subTest(array_input=array_input):
                numerical_array = np.array(object=array_input)

                self.assertEqual(first=expected_shape, second=numerical_array.shape)
                self.assertEqual(first=expected_size, second=numerical_array.size)
                self.assertEqual(first=array_input, second=numerical_array.tolist())

    def test_scalar_array_preserves_zero_dimensions(self):
        numerical_array = np.array(object=3.0)

        self.assertEqual(first=(), second=numerical_array.shape)
        self.assertEqual(first=0, second=numerical_array.ndim)
        self.assertEqual(first=1, second=numerical_array.size)
        self.assertEqual(first=3.0, second=numerical_array.tolist())

    def test_array_copy_and_list_conversion_own_independent_storage(self):
        source_samples = [[1.0, 2.0], [3.0, 4.0]]
        numerical_array = np.array(object=source_samples, dtype=float)
        copied_array = numerical_array.copy()
        exported_samples = numerical_array.tolist()

        source_samples[0][0] = 99.0
        exported_samples[0][1] = 99.0

        self.assertEqual(first=[[1.0, 2.0], [3.0, 4.0]], second=numerical_array.tolist())
        self.assertIsNot(expr1=numerical_array, expr2=copied_array)
        self.assertEqual(first=numerical_array.tolist(), second=copied_array.tolist())

    def test_array_from_array_copies_and_asarray_can_reuse(self):
        original_array = np.array(object=[1.0, 2.0], dtype=float)
        copied_array = np.array(object=original_array)

        self.assertIs(expr1=original_array, expr2=np.asarray(a=original_array, dtype=float))

        original_array[0] = 99.0

        self.assertEqual(first=[1.0, 2.0], second=copied_array.tolist())

    def test_asarray_converts_integer_storage_without_mutating_it(self):
        integer_array = np.array(object=[1, 2])
        floating_array = np.asarray(a=integer_array, dtype=float)

        self.assertEqual(first="i", second=integer_array.dtype.kind)
        self.assertEqual(first="f", second=floating_array.dtype.kind)
        self.assertEqual(first=[1.0, 2.0], second=floating_array.tolist())
        self.assertIsNot(expr1=integer_array, expr2=floating_array)

    def test_finite_masks_preserve_shape_and_boolean_dtype(self):
        numerical_array = np.array(object=[[1.0, float("nan")], [float("inf"), 4.0]])
        finite_mask = np.isfinite(numerical_array)

        self.assertEqual(first=(2, 2), second=finite_mask.shape)
        self.assertEqual(first="b", second=finite_mask.dtype.kind)
        self.assertEqual(first=[[True, False], [False, True]], second=finite_mask.tolist())
        self.assertFalse(expr=finite_mask.all())
        self.assertTrue(expr=np.isfinite(np.array(object=[], dtype=float)).all())

    def test_text_arrays_raise_instead_of_claiming_finiteness(self):
        text_array = np.array(object=["text"])

        with self.assertRaises(expected_exception=TypeError):
            np.isfinite(text_array)

    def test_vector_assignment_and_negative_indices(self):
        numerical_array = np.array(object=[1.0, 2.0], dtype=float)
        copied_array = numerical_array.copy()
        numerical_array[-1] = 3

        self.assertEqual(first=3.0, second=numerical_array[1])
        self.assertEqual(first=[1.0, 2.0], second=copied_array.tolist())

        with self.assertRaises(expected_exception=IndexError):
            numerical_array[2] = 0

    def test_ragged_and_unsupported_inputs_are_rejected(self):
        for ragged_input in ([[1], [2, 3]], [[1], 2]):
            with self.subTest(ragged_input=ragged_input), self.assertRaises(expected_exception=ValueError):
                np.array(object=ragged_input)

        with self.assertRaises(expected_exception=TypeError):
            np.array(object={"samples": [1.0]})

    def test_unsupported_modes_cannot_succeed_accidentally(self):
        integer_array = np.array(object=[1, 2])

        with self.assertRaises(expected_exception=NotImplementedError):
            np.array(object=[1], dtype=int)

        with self.assertRaises(expected_exception=NotImplementedError):
            np.asarray(a=integer_array, dtype=int)

        with self.assertRaises(expected_exception=NotImplementedError):
            np.sum(a=integer_array, dtype=int)

        with self.assertRaises(expected_exception=TypeError):
            integer_array[:]

        with self.assertRaises(expected_exception=TypeError):
            integer_array[True]

        with self.assertRaises(expected_exception=NotImplementedError):
            integer_array == integer_array

        with self.assertRaises(expected_exception=ValueError):
            bool(integer_array)

    def test_dot_rejects_shapes_and_nonnumeric_vectors(self):
        vector = np.array(object=[1.0, 2.0])

        for incompatible_array in (np.array(object=[1.0]), np.array(object=[1.0, 2.0, 3.0])):
            with self.subTest(shape=incompatible_array.shape), self.assertRaises(expected_exception=ValueError):
                np.dot(a=vector, b=incompatible_array)

        with self.assertRaises(expected_exception=NotImplementedError):
            np.dot(a=np.array(object=[[1.0, 2.0]]), b=vector)

        with self.assertRaises(expected_exception=NotImplementedError):
            np.dot(a=np.array(object=["1", "2"]), b=vector)

    def test_empty_vectors_have_zero_reductions(self):
        empty_vector = np.array(object=[], dtype=float)

        self.assertEqual(first=0.0, second=np.sum(a=empty_vector, dtype=float))
        self.assertEqual(first=0.0, second=np.dot(a=empty_vector, b=empty_vector))

    def test_seeded_vectors_match_independent_float_reductions(self):
        for seed in range(32):
            randomizer = random.Random(x=seed)
            left_samples = [randomizer.uniform(a=-100, b=100) for _ in range(seed)]
            right_samples = [randomizer.uniform(a=-100, b=100) for _ in range(seed)]
            left_array = np.array(object=left_samples, dtype=float)
            right_array = np.array(object=right_samples, dtype=float)
            expected_inner_product = math.fsum((left * right) for (left, right) in zip(left_samples, right_samples, strict=True))

            with self.subTest(seed=seed):
                self.assertAlmostEqual(first=expected_inner_product, second=np.dot(a=left_array, b=right_array), places=9)
                self.assertAlmostEqual(first=math.fsum(left_samples), second=np.sum(a=left_array, dtype=float), places=10)
