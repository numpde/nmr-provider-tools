"""Invalid preferences never silently select a lower disclosure level."""

import unittest
import os

from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityRejectionTests(unittest.TestCase):
    def test_only_exact_json_string_values_are_admitted(self):
        for value in (None, True, 0, 1.0, [], {}, ResultVerbosity.LOW):
            with self.subTest(value=value):
                with self.assertRaises(TypeError) as caught:
                    ResultVerbosity.parse(value)

                self.assertIn(type(value).__name__, str(caught.exception))

        for value in ("", "compact", "full", "MAX", " medium", "PRIVATE_CANARY"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    ResultVerbosity.parse(value)

                message = str(caught.exception)

                for member in ResultVerbosity:
                    self.assertIn(member.value, message)


    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_rejection_diagnostics_omit_input(self):
        with self.assertRaises(ValueError) as caught:
            ResultVerbosity.parse("PRIVATE_CANARY")

        self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
