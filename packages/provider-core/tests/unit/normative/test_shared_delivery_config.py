"""Outbox policy reload changes future admissions and retries without dropping work."""

import tempfile
import unittest
import asyncio
import contextlib
import io
import json
import pathlib
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryFull, DeliveryQueue, QueueLimits
from provider_core.shared_delivery_config import SharedDeliveryConfiguration
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from provider_core import delivery_discard
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class SharedDeliveryConfigurationTests(unittest.TestCase):
    def test_operator_can_inspect_held_legacy_rows_using_shared_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "settings.toml"

            config.write_text(
                '[outbox]\n'
                'provider_items = 2\n'
                'provider_bytes = 8192\n'
                'total_items = 4\n'
                'total_bytes = 16384\n'
                'retry_initial_seconds = 1\n'
                'retry_max_seconds = 5\n'
                'log_level = "INFO"\n'
            )

            queue = DeliveryQueue(root, limits=QueueLimits(2, 8192, 4, 16384))
            delivery_outcome = outcome()

            queue.enqueue(delivery_outcome)
            queue.hold_legacy_rows()
            queue.enqueue(outcome(suffix="1"))
            queue.close()
            output = io.StringIO()

            argv = [
                "delivery_discard",
                "--shared-config",
                str(config),
                "--state-directory",
                str(root),
                "--api-origin",
                delivery_outcome.api_origin,
                "--provider-ref",
                delivery_outcome.provider_ref,
                "--list-held"
            ]

            with patch("sys.argv", argv), contextlib.redirect_stdout(output):
                delivery_discard.main()

            inventory_report = json.loads(output.getvalue())

            self.assertEqual(inventory_report["held"][0]["execution_attempt_ref"], delivery_outcome.execution_attempt_ref)
            self.assertEqual(inventory_report["pending"][0]["execution_attempt_ref"], outcome(suffix="1").execution_attempt_ref)
            self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", output.getvalue())

    def test_atomic_reload_changes_admission_and_worker_retry_retains_last_good(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            (root / "state").mkdir(mode=0o700)
            settings_path = root / "settings.toml"

            def write(target, provider_item_limit, retry):
                target.write_text(
                    f'[outbox]\n'
                    f'provider_items = {provider_item_limit}\n'
                    f'provider_bytes = 8192\n'
                    f'total_items = 4\n'
                    f'total_bytes = 16384\n'
                    f'retry_initial_seconds = {retry}\n'
                    f'retry_max_seconds = 10\n'
                    f'log_level = "INFO"\n'
                )

            write(settings_path, 1, 1)
            live = SharedDeliveryConfiguration(settings_path)
            settings = live.read()
            notices = Mock()

            service = SharedDeliveryService(
                root / "state",
                limits=settings.limits,
                socket=SharedDeliverySocket(root / "delivery.sock", None),
                proxy_socket=root / "proxy.sock",
                ipc_io_timeout_seconds=1,
                retry_initial_seconds=1,
                retry_max_seconds=10,
                on_notice=notices,
                diagnostic_status=Mock(),
                live_configuration=live
            )

            service.queue = DeliveryQueue(root / "state", limits=settings.limits)

            self.addCleanup(service.queue.close)
            worker = SimpleNamespace(retry_initial_seconds=1, retry_max_seconds=10, policy_changed=asyncio.Event())
            service.workers[("https://api.example", "provider:example")] = worker

            fields = {
                "origin": "https://api.example",
                "profile": "dev-local",
                "topology": "dev-local",
                "provider_ref": "provider:example",
                "exchange_seconds": 1,
                "retirement_seconds": 1,
                "credential_file": "/gone/key"
            }

            (_, _, material) = api_settings_with_material(fields, Mock(return_value=encoded(document())))
            first = DeliveryHandoff(outcome(provider="provider:example"), material)
            second = DeliveryHandoff(outcome(provider="provider:example", suffix="1"), material)

            service.queue.enqueue_handoff(first)

            with self.assertRaises(DeliveryFull):
                service.queue.enqueue_handoff(second)

            replacement = root / "replacement.toml"

            write(replacement, 2, 3)
            replacement.replace(settings_path)
            service._reload()
            self.assertEqual(worker.retry_initial_seconds, 3)
            self.assertEqual(service.queue.enqueue_handoff(second), second.acceptance())
            settings_path.write_text("[outbox\n")
            service.next_reload = 0

            service._reload()
            self.assertEqual(service.queue.limits.provider_items, 2)
            self.assertEqual(worker.retry_initial_seconds, 3)
            self.assertEqual(len(service.queue.inventory("https://api.example", "provider:example")), 2)
            self.assertEqual(notices.call_args.args[1]["event"], "delivery_reload_failed")
            self.assertIsInstance(notices.call_args.args[0].__cause__, tomllib.TOMLDecodeError)
