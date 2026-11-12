"""Invalid preferences never silently select a lower disclosure level."""

import unittest

from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityRejectionTests(unittest.TestCase):
    def test_only_exact_json_string_values_are_admitted_without_echoing_input(self):
        for value in (None, True, 0, 1.0, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(TypeError) as caught:
                    ResultVerbosity.parse(value)
                self.assertIn(str(type(value)), str(caught.exception))
        for value in ("", "compact", "full", "MAX", " medium", "PRIVATE_CANARY"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    ResultVerbosity.parse(value)
                message = str(caught.exception)
                for member in ResultVerbosity:
                    self.assertIn(member.value, message)
                self.assertNotIn("PRIVATE_CANARY", message)
                self.assertTrue(caught.exception.__suppress_context__)
