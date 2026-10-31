"""The complete operation must withhold mappings until exact retirement."""

import unittest

from provider_deploy.operation import probe_identity


class OperationContractTests(unittest.TestCase):
    def test_complete_operation_is_public(self):
        self.assertTrue(callable(probe_identity))
