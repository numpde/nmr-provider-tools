"""Operator observations stay private and do not own publication."""

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
import os
import pathlib
from unittest.mock import Mock, patch

from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_logging import DeliveryNotices
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.process_status import check_status
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from tests.fakes.delivery import outcome
from tests.unit.normative.test_delivery_handoff import DeliveryHandoffTests


class QueueObservationTests(unittest.TestCase):
    def _queue_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = DeliveryQueue(pathlib.Path(directory), limits=QueueLimits(4, 16384, 8, 32768))
            material = DeliveryHandoffTests().material()
            first = DeliveryHandoff(outcome(provider="provider:example"), material)
            second = DeliveryHandoff(outcome(provider="provider:example", suffix="1"), material)

            queue.enqueue_handoff(first)
            queue.enqueue_handoff(second)
            queue.hold(second.outcome, "retained")
            (bindings, summary, failure) = queue.pending_overview()

            self.assertIsNone(failure)
            self.assertEqual(len(bindings), 1)
            self.assertEqual((summary["pending"], summary["held"]), (1, 1))
            self.assertEqual(summary["body_bytes"], len(first.outcome.body) + len(second.outcome.body))
            self.assertGreater(summary["publication_bytes"], 0)
            self.assertIsNotNone(summary["oldest_pending_created_at"])
            aggregate = summary

            queue._db.set_authorizer(
                lambda action, arg1, arg2, _db, _trigger:
                sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_FUNCTION) and (arg2 == "sum") else sqlite3.SQLITE_OK
            )

            (observed_bindings, observed_summary, failure) = queue.pending_overview()

            self.assertEqual(observed_bindings, bindings)
            self.assertIsNone(observed_summary)
            self.assertIsInstance(failure, sqlite3.Error)
            aggregate_failure = failure

            queue._db.set_authorizer(None)
            original = queue._db

            class FailedAggregate:
                def execute(self, query):
                    if "SUM(" in query:
                        raise sqlite3.OperationalError("PRIVATE_SCIENTIFIC_CANARY")

                    return original.execute(query)

            with patch.object(queue, "_db", FailedAggregate()):
                (_, summary, safe_failure) = queue.pending_overview()

            self.assertIsNone(summary)
            self.assertIs(type(safe_failure), sqlite3.OperationalError)
            stream = io.StringIO()
            notices = DeliveryNotices(stream, secrets=())

            notices.set_level("WARNING")
            notices(safe_failure, {"event": "delivery_overview_failed"})
            self.assertIn("delivery_overview_failed", stream.getvalue())
            self.assertIn("OperationalError", stream.getvalue())
            notices.close()
            queue.remove_published(first.outcome)
            self.assertEqual(queue.pending_overview()[1]["pending"], 0)
            queue.close()

            return (aggregate, first.outcome.job_ref, aggregate_failure, safe_failure, stream.getvalue())

    def test_aggregate_and_observation_failure_leave_the_queue_usable(self):
        self._queue_observation()

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_queue_observations_omit_identifiers_and_failure_text(self):
        (aggregate, job_ref, aggregate_failure, safe_failure, output) = self._queue_observation()

        self.assertNotIn("provider:example", json.dumps(aggregate))
        self.assertNotIn(job_ref, json.dumps(aggregate))
        self.assertEqual(aggregate_failure.args, ())
        self.assertEqual(safe_failure.args, ())
        self.assertIsNone(safe_failure.__context__)
        self.assertNotIn("PRIVATE_SCIENTIFIC_CANARY", output)


class ServiceObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregate_failure_is_reported_without_stopping_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            for name in ("state", "ipc", "status"):
                (root / name).mkdir(mode=0o700)

            stream = io.StringIO()
            notices = DeliveryNotices(stream, secrets=())

            notices.set_level("WARNING")

            service = SharedDeliveryService(
                root / "state",
                limits=QueueLimits(4, 16384, 8, 32768),
                socket=SharedDeliverySocket(root / "ipc/http.sock", None),
                proxy_socket=root / "absent.sock",
                ipc_io_timeout_seconds=1,
                retry_initial_seconds=1,
                retry_max_seconds=1,
                on_notice=notices,
                diagnostic_status=notices.status,
                status_directory=root / "status"
            )

            task = asyncio.create_task(service.run())

            try:
                await asyncio.wait_for(service.ready.wait(), 2)

                service.queue._db.set_authorizer(
                    lambda action, arg1, arg2, _db, _trigger:
                    sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_FUNCTION) and (arg2 == "sum") else sqlite3.SQLITE_OK
                )

                service.overview_at = 0

                service.changed.set()

                for _ in range(100):
                    if "delivery_overview_failed" in stream.getvalue():
                        break

                    await asyncio.sleep(0.01)
                else:
                    self.fail("missing queue observation warning")

                self.assertIn("sqlite_errorcode", stream.getvalue())
                self.assertFalse(service.inbox.failed.is_set())
                self.assertIsNone(service.overview)
                service.queue._db.set_authorizer(None)
                delivery_outcome = outcome(provider="provider:example")
                material = DeliveryHandoffTests().material()

                client = SharedDeliveryClient(
                    DeliveryEndpoint(delivery_outcome.api_origin, delivery_outcome.provider_ref, root / "ipc/http.sock"),
                    material,
                    exchange_seconds=1,
                    retirement_seconds=1
                )

                self.assertEqual(await client.submit(delivery_outcome), DeliveryHandoff(delivery_outcome, material).acceptance())
                await client.retire()
            finally:
                service.request_stop()
                await asyncio.wait_for(task, 5)
                notices.close()

    async def test_snapshot_write_failure_does_not_stop_the_queue_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            for name in ("state", "ipc", "status"):
                (root / name).mkdir(mode=0o700)

            notice = Mock()

            service = SharedDeliveryService(
                root / "state",
                limits=QueueLimits(4, 16384, 8, 32768),
                socket=SharedDeliverySocket(root / "ipc/http.sock", None),
                proxy_socket=root / "absent.sock",
                ipc_io_timeout_seconds=1,
                retry_initial_seconds=1,
                retry_max_seconds=1,
                on_notice=notice,
                diagnostic_status=Mock(),
                status_directory=root / "status"
            )

            task = asyncio.create_task(service.run())

            try:
                await asyncio.wait_for(service.ready.wait(), 2)

                for _ in range(100):
                    status = check_status(root / "status/status.json", "delivery")

                    if "details" in status:
                        break

                    await asyncio.sleep(0.01)

                self.assertEqual(status["details"]["queue"]["pending"], 0)
                self.assertIsNone(status["details"]["queue"]["oldest_pending_age_seconds"])
                before = (root / "status/status.json").read_bytes()

                with patch.object(service.process_status, "refresh", side_effect=OSError("status unavailable")):
                    service.changed.set()

                    for _ in range(100):
                        if any((call.args[1].get("event") == "delivery_status_failed") for call in notice.call_args_list):
                            break

                        await asyncio.sleep(0.01)
                    else:
                        self.fail("snapshot failure was not reported")

                    self.assertFalse(task.done())
                    self.assertFalse(service.inbox.failed.is_set())
                    self.assertEqual((root / "status/status.json").read_bytes(), before)
                    material = DeliveryHandoffTests().material()
                    delivery_outcome = outcome(provider="provider:example")

                    client = SharedDeliveryClient(
                        DeliveryEndpoint(delivery_outcome.api_origin, delivery_outcome.provider_ref, root / "ipc/http.sock"),
                        material,
                        exchange_seconds=1,
                        retirement_seconds=1
                    )

                    self.assertEqual(await client.submit(delivery_outcome), DeliveryHandoff(delivery_outcome, material).acceptance())
                    await client.retire()
                    self.assertEqual(service.queue.next_pending_handoff(delivery_outcome.api_origin, delivery_outcome.provider_ref).outcome, delivery_outcome)
            finally:
                service.request_stop()
                await asyncio.wait_for(task, 5)
