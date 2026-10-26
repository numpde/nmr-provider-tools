"""Invalid preferences never silently select a lower disclosure level."""

import unittest

from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityRejectionTests(unittest.TestCase):
    def test_only_exact_json_string_values_are_admitted_without_echoing_input(self):
        for value in (None, True, 0, 1.0, [], {}, "", "compact", "full", "MAX", " medium", "PRIVATE_CANARY"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "low, medium or max") as caught:
                ResultVerbosity.parse(value)
            self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
