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


class ProcessWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_factory_resolution_retains_host_for_cleanup(self):
        from provider_core.process_worker import _Worker

        worker = _Worker()
        options = {
            "input_bytes": 2048,
            "result_bytes": 2048,
            "state_values": 1,
            "progress_records": 1,
            "initialization_seconds": 2,
            "retirement_seconds": 2,
        }

        with self.assertRaises(expected_exception=ModuleNotFoundError):
            await worker.command({
                "action": "start",
                "host_options": options,
                "constructor": "not_installed_scientific_factory:create",
                "contribution_type": "provider_core.scientific:Contribution",
                "startup": {},
                "requirements": (),
            })

        observation = await worker.command({"action": "retire"})
        self.assertTrue(observation.settled)
        self.assertEqual((), observation.failures)


if __name__ == "__main__":
    unittest.main()
