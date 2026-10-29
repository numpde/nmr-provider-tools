"""An installed core is independent of scientific and repository packages."""

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallationContractTests(unittest.TestCase):
    def test_binding_cli_refuses_private_parser_text_without_printing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.toml"
            path.write_text('["PRIVATE_BINDING_KEY_CANARY"]\nx=1\n["PRIVATE_BINDING_KEY_CANARY"]\nx=2\n')
            path.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "provider_core.deployment_binding", "paths",
                 "--definition", str(path), "--all"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertNotIn("PRIVATE_BINDING_KEY_CANARY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("UTF-8 TOML", result.stderr)

    def test_delivery_client_handoff_without_execution_controller_modules(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).with_name("client_only.py"))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)

    def test_runtime_dependencies_load_and_perform_native_signing(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import httpx, http_sf; "
                "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; "
                "key = Ed25519PrivateKey.generate(); "
                "key.public_key().verify(key.sign(b'contained runtime'), b'contained runtime')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_wheel_contains_only_the_declared_core(self):
        distribution = importlib.metadata.distribution("provider-core")
        payload = {str(path) for path in distribution.files if ".dist-info/" not in str(path)}
        self.assertEqual(
            payload,
            {
                "provider_core/__init__.py",
                "provider_core/bound_http_proxy.py",
                "provider_core/provider_proxy.py",
                "provider_core/deployment_binding.py",
                "provider_core/deployment_route.py",
                "provider_core/container_guard.py",
                "provider_core/runner.py",
                "provider_core/runner_protocol.py",
                "provider_core/runner_session.py",
                "provider_core/async_runner_session.py",
                "provider_core/task_boundary.py",
                "provider_core/process_status.py",
                "provider_core/result_verbosity.py",
                "provider_core/chat.py",
                "provider_core/conversation.py",
                "provider_core/credential.py",
                "provider_core/failure_projection.py",
                "provider_core/hello.py",
                "provider_core/job_input.py",
                "provider_core/job_feed.py",
                "provider_core/paging.py",
                "provider_core/attempt_inventory.py",
                "provider_core/signing.py",
                "provider_core/transport.py",
                "provider_core/decoded_body.py",
                "provider_core/network.py",
                "provider_core/api.py",
                "provider_core/attempt_response.py",
                "provider_core/attempt_request.py",
                "provider_core/attempt_record.py",
                "provider_core/api_config.py",
                "provider_core/delivery_client.py",
                "provider_core/delivery_config.py",
                "provider_core/delivery_discard.py",
                "provider_core/delivery_ipc.py",
                "provider_core/delivery_logging.py",
                "provider_core/delivery_main.py",
                "provider_core/delivery_service.py",
                "provider_core/delivery_store.py",
                "provider_core/delivery_worker.py",
                "provider_core/diagnostics.py",
                "provider_core/producer_checkpoint.py",
                "provider_core/startup_file.py",
                "provider_core/unix_http.py",
                "provider_core/ownership.py",
                "provider_core/lifetime.py",
                "provider_core/starting.py",
                "provider_core/analysis.py",
                "provider_core/execution.py",
                "provider_core/controller.py",
                "provider_core/response_json.py",
                "provider_core/upload_response.py",
                "provider_core/uploads.py",
                "provider_core/upload_file.py",
            },
        )
        self.assertEqual(
            set(distribution.requires), {f'{requirement}; extra == "api"' for requirement in (
                "cryptography==50.0.0", "http-sf==1.2.1", "httpx==0.28.1", "httpcore==1.0.9", "h11==0.16.0", "tomli-w==1.2.0",
            )}
        )

    def test_core_imports_without_scientific_or_repository_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import importlib.util; "
                "forbidden = {'tests','packages','repository_checks','tools','rdkit','torch','nmrnet_provider','nmrnet_inference'}; "
                "assert all(importlib.util.find_spec(name) is None for name in forbidden); "
                "import provider_core.failure_projection, provider_core.runner, provider_core.runner_protocol, provider_core.runner_session, provider_core.process_status; "
                "import provider_core.chat, provider_core.conversation, provider_core.credential, provider_core.hello, provider_core.job_input, provider_core.job_feed, provider_core.paging, provider_core.attempt_inventory, provider_core.signing, provider_core.transport, provider_core.network, provider_core.api, provider_core.uploads, provider_core.attempt_response, provider_core.response_json, provider_core.attempt_record, provider_core.starting, provider_core.analysis, provider_core.execution, provider_core.controller, provider_core.api_config, provider_core.delivery_client, provider_core.delivery_config, provider_core.delivery_discard, provider_core.delivery_ipc, provider_core.delivery_logging, provider_core.delivery_main, provider_core.delivery_service, provider_core.delivery_store, provider_core.delivery_worker, provider_core.diagnostics, provider_core.producer_checkpoint, provider_core.startup_file, provider_core.unix_http; import sys; "
                "assert not any(n.split('.')[0] in forbidden for n in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_container_marker_rejects_operational_import(self):
        environment = dict(os.environ)
        # silent-defaults: allow-pop-default -- Remove the inherited marker only to prove the missing-marker refusal.
        environment.pop("PROVIDER_CORE_IN_CONTAINER", None)
        for module in ("job_input", "chat", "conversation", "credential", "hello", "failure_projection", "runner", "runner_protocol", "runner_session", "process_status"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", f"import provider_core.{module}"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("contained launcher", result.stderr)

    def test_runner_rejects_empty_selected_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            (tests / "unit").mkdir(parents=True)
            for path in (tests / "__init__.py", tests / "unit/__init__.py"):
                path.write_text("")
            shutil.copyfile(Path(__file__).parents[1] / "run_suite.py", tests / "run_suite.py")
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(tests / "run_suite.py"), "unit", "test*.py"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains no tests", result.stderr)
