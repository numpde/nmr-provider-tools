"""The SDK consumes a qualified source archive through the same public updater."""

from tools.container_guard import require_container

require_container()

import unittest

from bootstrap.acquisition.acquisition_tool.source_update import read_pin, verify
from tests.repository.quality.sources import ROOT


class AcquisitionSourceTests(unittest.TestCase):
    def test_checked_in_source_matches_its_reviewed_pin(self):
        manifest = verify(ROOT / "bootstrap/acquisition")
        pin = read_pin(ROOT / "requirements/provider-tools-source.json")
        self.assertEqual(manifest["release_version"], pin["version"])
