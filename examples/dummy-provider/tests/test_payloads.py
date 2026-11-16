"""Exercise array-valued science through the SDK's JSON admission boundary."""

from provider_core.container_guard import require_container

require_container()

import unittest

import mock_numpy as np

from provider_core.payload import PayloadRejected

from dummy_provider import science
from dummy_provider.payloads import SamplesCodec


class ArrayPayloadTests(unittest.TestCase):
    def test_both_scientific_inputs_round_trip_as_arrays(self):
        for input_type in (science.PreparationInput, science.SumInput):
            with self.subTest(input_type=input_type):
                codec = SamplesCodec(value_type=input_type)
                scientific_input = input_type(samples=np.array(object=[1.0, 2.0], dtype=float))
                encoded_input = codec.encode(value=scientific_input)
                decoded_input = codec.decode(payload=encoded_input)

                self.assertEqual(first=b'{"samples":[1.0,2.0]}', second=encoded_input)
                self.assertIsInstance(obj=decoded_input.samples, cls=np.ndarray)
                self.assertEqual(first=[1.0, 2.0], second=decoded_input.samples.tolist())
                self.assertIsNot(expr1=scientific_input.samples, expr2=decoded_input.samples)

    def test_sdk_rejects_malformed_sample_documents_before_array_construction(self):
        codec = SamplesCodec(value_type=science.PreparationInput)

        for malformed_payload in (
            b'{"samples":[true]}',
            b'{"samples":["1"]}',
            b'{"samples":[[1]]}',
            b'{"samples":[NaN]}',
            b'{"samples":[1],"samples":[2]}',
            b'{"samples":[1],"extra":2}',
        ):
            with self.subTest(malformed_payload=malformed_payload), self.assertRaises(expected_exception=PayloadRejected):
                codec.decode(payload=malformed_payload)

    def test_another_python_type_is_rejected_before_field_access(self):
        codec = SamplesCodec(value_type=science.PreparationInput)

        with self.assertRaises(expected_exception=TypeError):
            codec.encode(value=object())

    def test_mutated_nonfinite_samples_are_refused_at_the_wire_boundary(self):
        codec = SamplesCodec(value_type=science.PreparationInput)
        scientific_input = science.PreparationInput(samples=np.array(object=[1.0], dtype=float))
        scientific_input.samples[0] = float("nan")

        with self.assertRaises(expected_exception=TypeError):
            codec.encode(value=scientific_input)
