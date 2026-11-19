"""Exercise installed scientific entrypoint resolution without a model import."""

from __future__ import annotations

import unittest

from provider_core.process_worker import _installed_member
from provider_core.scientific import Contribution


class ProcessWorkerTests(unittest.TestCase):
    def test_installed_contribution_class_is_resolved_by_module_and_name(self):
        self.assertIs(Contribution, _installed_member("provider_core.scientific:Contribution"))

    def test_nonportable_local_construction_is_refused(self):
        with self.assertRaises(ValueError):
            _installed_member("my_module:outer.<locals>.factory")


if __name__ == "__main__":
    unittest.main()
