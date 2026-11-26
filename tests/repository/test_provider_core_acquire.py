"""SDK bootstrap delegates complete environment verification to independent Wheelhouse."""

from tools.container_guard import require_container

require_container()

import hashlib
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdk_build import provider_core_acquire


class ProviderCoreAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.pool = self.root / "pool"

        self.pool.mkdir()
        self.events = self.root / "events"
        self.tool = self.pool / "independent-wheelhouse.pyz"

        # Observe process arguments and distinct complete locks without
        # emulating Wheelhouse's package selection or wheel verification.
        self.tool.write_text(
            f"import json, pathlib, sys\n"
            f"events = pathlib.Path({str(self.events)!r})\n"
            f"with events.open('a') as output:\n"
            f"    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"lock = pathlib.Path(sys.argv[sys.argv.index('--lock') + 1])\n"
            f"raise SystemExit(int(lock.read_text()))\n"  # machine-string: Generate a subprocess fixture with its private event path embedded as a Python string literal.
        )

        self.pin = {
            "name": self.tool.name,
            "url": "https://example.invalid/independent-wheelhouse.pyz",
            "sha256": hashlib.sha256(self.tool.read_bytes()).hexdigest(),
        }

        self.locks = [
            (
                self.root / f"{role}.wheelhouse.json"  # machine-string: Each synthetic role has a distinct complete lock filename.
            )
            for role in ("runtime", "build", "test", "bootstrap")
        ]

        for lock in self.locks:
            lock.write_text("0")

    def commands(self):
        import json

        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_explicit_acquisition_uses_every_complete_lock_without_resolution(self):
        with patch.object(provider_core_acquire, "require_download_proxy", side_effect=AssertionError("warm tool should need no fetch")):
            provider_core_acquire.acquire_environments(self.pool, self.locks, self.pin)

        self.assertEqual(
            [["acquire", "--lock", str(lock), "--pool", str(self.pool)] for lock in self.locks],
            self.commands(),
        )

    def test_offline_check_includes_bootstrap_environment(self):
        provider_core_acquire.check_environments(self.pool, self.locks, self.pin)

        self.assertEqual(
            [["check", "--lock", str(lock), "--pool", str(self.pool)] for lock in self.locks],
            self.commands(),
        )

    def test_missing_tool_fails_before_execution(self):
        self.tool.unlink()

        with self.assertRaisesRegex(FileNotFoundError, "pinned Wheelhouse is missing"):
            provider_core_acquire.check_environments(self.pool, self.locks, self.pin)

        self.assertFalse(self.events.exists())

    def test_corrupt_tool_is_neither_executed_nor_replaced(self):
        self.tool.write_bytes(b"changed tool")

        with self.assertRaises(ValueError):
            provider_core_acquire.check_environments(self.pool, self.locks, self.pin)

        with self.assertRaises(ValueError):
            provider_core_acquire.acquire_environments(self.pool, self.locks, self.pin)

        self.assertEqual(b"changed tool", self.tool.read_bytes())
        self.assertFalse(self.events.exists())

    def test_failed_environment_verification_stops_before_later_roles(self):
        self.locks[1].write_text("1")

        with self.assertRaises(subprocess.CalledProcessError):
            provider_core_acquire.check_environments(self.pool, self.locks, self.pin)

        self.assertEqual(2, len(self.commands()))

    def test_failed_acquisition_is_not_retried_with_another_source(self):
        self.locks[0].write_text("1")

        with self.assertRaises(subprocess.CalledProcessError):
            provider_core_acquire.acquire_environments(self.pool, self.locks, self.pin)

        self.assertEqual(1, len(self.commands()))


if __name__ == "__main__":
    unittest.main()
