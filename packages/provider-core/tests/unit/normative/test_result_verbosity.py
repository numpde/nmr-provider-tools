"""Consumers share exact names without inheriting scientific field policy or defaults."""

import unittest

from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityTests(unittest.TestCase):
    def test_three_names_parse_to_their_own_members(self):
        self.assertEqual([choice.value for choice in ResultVerbosity], ["low", "medium", "max"])
        for choice in ResultVerbosity:
            with self.subTest(choice=choice):
                self.assertIs(ResultVerbosity.parse(choice.value), choice)
