"""Owner mapping and cleanup receipts use the qualified probe outcome."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from acquisition_tool import identity
from linux_idmap import IdMap, IdRange
from linux_idmap.probe import ProbeFailure, ProbeResult, Problem, ResourceState, Retirement


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt = Path(temporary.name) / "retired"
        self.argv = ["--endpoint", "unix:///fixture/engine.sock", "--image", "fixture:local",
                     "--host-uid", "1000", "--host-gid", "2000",
                     "--retired-receipt", str(self.receipt)]
        self.observation = ProbeResult(IdMap([IdRange(0, 1000, 1)]),
                                       IdMap([IdRange(0, 2000, 1)]), "a" * 64)

    def run_entrypoint(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            status = identity.main(self.argv)
        return status, output.getvalue(), diagnostic.getvalue()

    @patch.object(identity, "probe_identity")
    def test_maps_host_owner_to_container_zero_after_probe_retires(self, probe):
        probe.return_value = self.observation
        self.assertEqual((0, "0 0\n", ""), self.run_entrypoint())
        self.assertEqual(b"retired\n", self.receipt.read_bytes())
        request = probe.call_args.args[0]
        self.assertEqual((0, 0), (request.user_uid, request.user_gid))

    @patch.object(identity, "probe_identity")
    def test_unmapped_owner_failure_still_records_completed_probe_retirement(self, probe):
        probe.return_value = self.observation
        self.argv[self.argv.index("1000")] = "1001"
        status, output, diagnostic = self.run_entrypoint()
        self.assertEqual((2, ""), (status, output))
        self.assertIn("not mapped", diagnostic)
        self.assertEqual(b"retired\n", self.receipt.read_bytes())

    @patch.object(identity, "probe_identity")
    def test_request_refusal_records_no_dispatch_without_traceback(self, probe):
        self.argv[self.argv.index("unix:///fixture/engine.sock")] = "tcp://fixture:2375"
        status, output, diagnostic = self.run_entrypoint()
        self.assertEqual((2, ""), (status, output))
        probe.assert_not_called()
        self.assertNotIn("Traceback", diagnostic)
        self.assertEqual(b"retired\n", self.receipt.read_bytes())

    @patch.object(identity, "probe_identity")
    def test_unresolved_probe_keeps_retirement_receipt_absent(self, probe):
        failure = ProbeFailure(Problem("engine", "fixture failure"), Retirement(ResourceState.UNRESOLVED))
        probe.side_effect = failure
        self.assertEqual((2, ""), self.run_entrypoint()[:2])
        self.assertFalse(self.receipt.exists())
        with self.assertRaises(ProbeFailure) as caught:
            identity.mapped_owner("unix:///fixture/engine.sock", "fixture:local", 1000, 2000)
        self.assertIs(failure, caught.exception)

    @patch.object(identity, "probe_identity")
    def test_safe_probe_state_does_not_erase_unresolved_native_client(self, probe):
        for primary in (True, False):
            with self.subTest(primary=primary):
                cleanup = Problem("client_cleanup", "fixture client unresolved")
                problem = cleanup if primary else Problem("engine", "fixture failed")
                remaining = () if primary else (cleanup,)
                probe.side_effect = ProbeFailure(problem, Retirement(ResourceState.RETIRED, problems=remaining))
                status, output, diagnostic = self.run_entrypoint()
                self.assertEqual((2, ""), (status, output))
                self.assertIn("fixture client unresolved", diagnostic)
                self.assertFalse(self.receipt.exists())

    @patch.object(identity, "probe_identity")
    def test_cancellation_is_separate_from_ordinary_probe_failure(self, probe):
        def cancelled(_request, *, cancel):
            cancel.request(signal.SIGTERM, None)
            raise ProbeFailure(Problem("cancelled", "fixture cancelled"), Retirement(ResourceState.RETIRED))
        probe.side_effect = cancelled
        self.assertEqual((143, ""), self.run_entrypoint()[:2])
        self.assertEqual(b"retired\n", self.receipt.read_bytes())


if __name__ == "__main__":
    unittest.main()
