"""Check process launch preflight before any child or socket effects."""

from __future__ import annotations

import unittest

from provider_core.process_host import ProcessExecutionHost
from provider_core.scientific import Contribution


class ProcessHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_contribution_class_is_refused_before_child_spawn(self):
        class LocalContribution(Contribution):
            pass

        host = ProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=1,
            initialization_seconds=2,
            retirement_seconds=2,
        )

        with self.assertRaises(expected_exception=ValueError):
            await host.start(
                contribution_type=LocalContribution,
                constructor="provider_core.scientific:Contribution",
                startup={},
                requirements=frozenset(),
            )

        self.assertIsNone(host.process)
        self.assertIsNone(host.channel)
        self.assertIsNone(host.stderr)


if __name__ == "__main__":
    unittest.main()
