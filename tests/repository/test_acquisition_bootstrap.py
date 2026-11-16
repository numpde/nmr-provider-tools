"""The SDK consumes a qualified source archive through the same public updater."""

from tools.container_guard import require_container

require_container()

import unittest

from bootstrap.acquisition.acquisition_tool.source_update import read_pin, verify
from tests.repository.quality.sources import ROOT


class AcquisitionSourceTests(unittest.TestCase):
    def test_checked_in_snapshot_verifies_and_its_version_matches_the_pin(self):
        """Check snapshot integrity and version labels; the archive digest is not checked here."""
        manifest = verify(ROOT / "bootstrap/acquisition")
        pin = read_pin(ROOT / "requirements/provider-tools-source.json")

        self.assertEqual(manifest["release_version"], pin["version"])
