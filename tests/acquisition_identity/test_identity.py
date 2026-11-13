"""Owner mapping and cleanup receipts use the qualified probe outcome."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acquisition_tool import identity_main as identity
from linux_idmap import IdMap, IdRange
from linux_idmap.probe import ProbeFailure, ProbeResources, ProbeResult, Problem, ResourceState, Retirement


class IdentityTests(unittest.TestCase):
    def test_isolated_wrapper_uses_its_bundle_and_preserves_arguments_streams_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "bundle/acquisition_tool"
            package.mkdir(parents=True)
            for name in ("__init__.py", "identity.py"):
                shutil.copyfile(Path(identity.__file__).with_name(name), package / name)
            (package / "identity_main.py").write_text(
                'import json, sys\n'
                'if __name__ == "__main__":\n'
                '    sys.stdout.write(json.dumps(sys.argv))\n'
                '    sys.stderr.write("probe diagnostic\\n")\n'
                '    raise SystemExit(17)\n'
            )
            poison = root / "poison/acquisition_tool"
            poison.mkdir(parents=True)
            (poison / "__init__.py").write_text('raise RuntimeError("Host package loaded")\n')
            entrypoint = package / "identity.py"
            arguments = ["--image", "fixture image", "$(touch sentinel)"]
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(entrypoint), *arguments],
                env=os.environ | {"PYTHONPATH": str(poison.parent)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(17, result.returncode, msg=result.stderr)
            self.assertEqual([str(entrypoint), *arguments], json.loads(result.stdout))
            self.assertEqual("probe diagnostic\n", result.stderr)

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

    def mapping_arguments(self, *projections):
        arguments = ["--endpoint", "unix:///fixture/engine.sock", "--image", "fixture:local",
                     "--interpreter", "/usr/local/bin/python3.12", "--user-uid", "65532", "--user-gid", "65532",
                     "--memory-bytes", "33554432", "--pids-limit", "4", "--nano-cpus", "250000000",
                     "--retired-receipt", str(self.receipt)]
        for projection in projections:
            arguments.extend(("--map", *projection))
        return arguments

    @patch.object(identity, "probe_identity")
    def test_mixed_directions_and_multiple_ids_share_one_bounded_observation(self, probe):
        probe.return_value = ProbeResult(
            IdMap([IdRange(0, 1000, 1), IdRange(1, 100000, 65536)]),
            IdMap([IdRange(0, 2000, 1), IdRange(1, 200000, 65536)]), "a" * 64,
        )
        self.argv = self.mapping_arguments(("uid", "forward", "1000"),
                                           ("gid", "inverse", "2000"), ("uid", "forward", "65532"))
        self.assertEqual((0, "100999 0 165531\n", ""), self.run_entrypoint())
        probe.assert_called_once()
        request = probe.call_args.args[0]
        self.assertEqual(("unix:///fixture/engine.sock", "fixture:local", "/usr/local/bin/python3.12"),
                         (request.endpoint, request.image, request.interpreter))
        self.assertEqual((65532, 65532), (request.user_uid, request.user_gid))
        self.assertEqual(ProbeResources(33554432, 4, 250000000), request.resources)
        self.assertEqual(b"retired\n", self.receipt.read_bytes())

    @patch.object(identity, "probe_identity")
    def test_projection_observes_the_selected_user_namespace(self, probe):
        probe.return_value = self.observation
        self.argv = self.mapping_arguments(("uid", "forward", "0")) + ["--userns", "host"]
        self.assertEqual((0, "1000\n", ""), self.run_entrypoint())
        self.assertEqual("host", probe.call_args.args[0].userns_mode)

    @patch.object(identity, "probe_identity")
    def test_failed_later_projection_never_releases_partial_values(self, probe):
        probe.return_value = self.observation
        self.argv = self.mapping_arguments(("uid", "forward", "0"), ("gid", "inverse", "999"))
        status, output, diagnostic = self.run_entrypoint()
        self.assertEqual((2, ""), (status, output))
        self.assertIn("not mapped", diagnostic)
        self.assertEqual(b"retired\n", self.receipt.read_bytes())

    @patch.object(identity, "probe_identity")
    def test_projection_policy_is_complete_and_unambiguous_before_dispatch(self, probe):
        complete = self.mapping_arguments(("uid", "forward", "0"))
        missing_ceiling = complete.copy()
        position = missing_ceiling.index("--pids-limit")
        del missing_ceiling[position:position + 2]
        cases = (missing_ceiling, complete + ["--host-uid", "1000", "--host-gid", "2000"],
                 self.mapping_arguments(("uid", "repair", "0")),
                 self.mapping_arguments(("pid", "forward", "0")),
                 self.mapping_arguments(("uid", "forward", "-1")))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.receipt.unlink(missing_ok=True)
                self.argv = arguments
                self.assertEqual((2, ""), self.run_entrypoint()[:2])
                self.assertEqual(b"retired\n", self.receipt.read_bytes())
        probe.assert_not_called()

    @patch.object(identity, "probe_identity")
    def test_projection_output_follows_receipt_and_restored_handlers(self, probe):
        probe.return_value = self.observation
        previous = signal.getsignal(signal.SIGTERM)
        test = self

        class GuardedOutput(io.StringIO):
            def write(self, value):
                test.assertEqual(b"retired\n", test.receipt.read_bytes())
                test.assertEqual(previous, signal.getsignal(signal.SIGTERM))
                return super().write(value)

        output = GuardedOutput()
        with redirect_stdout(output):
            self.assertEqual(0, identity.main(self.argv))
        self.assertEqual("0 0\n", output.getvalue())

    @patch.object(identity, "probe_identity")
    def test_signal_during_handler_restoration_cannot_release_values(self, probe):
        probe.return_value = self.observation
        install = signal.signal
        previous = signal.getsignal(signal.SIGTERM)

        def interrupted_restoration(number, handler):
            if number == signal.SIGTERM and handler == previous:
                signal.raise_signal(signal.SIGTERM)
            return install(number, handler)

        with patch.object(identity.signal, "signal", side_effect=interrupted_restoration):
            self.assertEqual((143, ""), self.run_entrypoint()[:2])
        self.assertEqual(b"retired\n", self.receipt.read_bytes())
        self.assertEqual(previous, signal.getsignal(signal.SIGTERM))

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
        status, output, diagnostic = self.run_entrypoint()
        self.assertEqual((2, ""), (status, output))
        self.assertFalse(self.receipt.exists())
        probe.assert_called_once()
        self.assertIn("fixture failure", diagnostic)
        self.assertNotIn("Traceback", diagnostic)

    @patch.object(identity, "probe_identity")
    def test_safe_probe_state_does_not_erase_unresolved_native_client(self, probe):
        for primary in (True, False):
            with self.subTest(primary=primary):
                cleanup = Problem("client_cleanup", "fixture client unresolved")
                problem = cleanup if primary else Problem("engine", "fixture failed")
                remaining = (cleanup,)
                if primary:
                    remaining = ()
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
