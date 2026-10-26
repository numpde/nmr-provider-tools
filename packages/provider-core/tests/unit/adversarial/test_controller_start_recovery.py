"""START replay cannot cross received acknowledgement or unsettled cleanup."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
import asyncio
import errno
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpcore

from provider_core.attempt_record import AcknowledgedAttempt, StartPending
from provider_core.checkpoint_store import CheckpointRefused
from provider_core.controller import AttemptController, ControllerFailed, StartRecovery
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.starting import StartFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import start_response
from tests.fakes.controller import SingleRetirementSession, StoreCallBarrier, cancel_task, empty_store
from tests.fakes.api import api


class ControllerStartRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- The ordinary injected failure is ControllerFailed; recovery scenarios explicitly choose another type.
    async def run_failure(self, start_reply, *, recovery, expected_exception=ControllerFailed):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(start_reply)
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=recovery,
            )
            try:
                with self.assertRaises(expected_exception) as caught:
                    await controller.run(record())
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertIsInstance((await store.inventory())[0], StartPending)
                return caught.exception, controller
            finally:
                await store.close()

    async def test_permanent_reply_or_unsettled_cleanup_never_retries(self):
        for response in (
            reply(b"denied", status=401),
            reply(b"conflict", status=409),
            reply(b"invalid success"),
            reply(b"maintenance", status=503, headers=response_headers(status=503, topology=b"web")),
            ExchangeFailed("Certificate rejected", primary=ssl.SSLCertVerificationError()),
            ExchangeFailed("Cleanup failed", primary=httpcore.ReadTimeout(), cleanup_failures=(OSError(),)),
        ):
            with self.subTest(response=response):
                retry = Mock()
                failure, _ = await self.run_failure(response, recovery=StartRecovery(0.01, retry, Mock()))
                self.assertIsInstance(failure.cause, StartFailed)
                retry.assert_not_called()

    async def test_received_acknowledgement_write_failure_never_resends(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            pending, retry = record(), Mock()
            transport = Transport(reply(start_response()))
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=StartRecovery(0.001, retry, Mock()),
            )
            try:
                with (
                    patch.object(store._database, "acknowledge_start", side_effect=OSError(errno.EIO, "PRIVATE_DISK")),
                    self.assertRaises(ControllerFailed) as caught,
                ):
                    await controller.run(pending)
                failure = caught.exception.cause
                self.assertIsInstance(failure.acknowledgement, AcknowledgedAttempt)
                self.assertIsNone(failure.api_failure)
                self.assertIs(controller.operation.last_failure, failure)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertEqual(await store.inventory(), (pending,))
                retry.assert_not_called()
            finally:
                await store.close()

    async def test_cancelled_acknowledgement_write_settles_original_future_without_resend_or_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(reply(start_response()))
            retry = Mock()
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=StartRecovery(0.001, retry, Mock()),
            )
            barrier = StoreCallBarrier(lambda: None)
            original_replace = store._database.acknowledge_start

            def held_replace(*args, **kwargs):
                barrier.call()
                return original_replace(*args, **kwargs)

            task = None
            try:
                with patch.object(store._database, "acknowledge_start", side_effect=held_replace) as writes:
                    task = asyncio.create_task(controller.run(record()))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    starter = controller.operation
                    self.assertIsInstance(starter.last_failure.acknowledgement, AcknowledgedAttempt)
                    self.assertIs(store.pending_owner, starter)
                    barrier.release.set()
                    acknowledged = await controller.settle()
                    self.assertEqual(writes.call_count, 1)
                self.assertEqual(await store.inventory(), (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                retry.assert_not_called()
            finally:
                barrier.release.set()
                if task is not None:
                    await cancel_task(task)
                await store.close()

    async def test_cancelled_backoff_keeps_original_start_without_resending(self):
        cancellation = asyncio.CancelledError()
        with patch("provider_core.controller.asyncio.sleep", new=AsyncMock(side_effect=cancellation)):
            failure, _ = await self.run_failure(
                reply(b"maintenance", status=503),
                recovery=StartRecovery(0.01, Mock(), Mock()),
                expected_exception=asyncio.CancelledError,
            )
        self.assertIs(failure, cancellation)
        self.assertIsInstance(failure.__context__, StartFailed)

    async def test_retirement_and_late_evidence_precede_replay(self):
        for late_evidence in ("certificate", "cleanup", "availability"):
            with self.subTest(late_evidence=late_evidence):
                await self.check_retirement(late_evidence)

    async def check_retirement(self, late_evidence):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            lost = ExchangeFailed("Unconfirmed START", primary=httpcore.ReadTimeout())
            transport = Transport(lost, reply(start_response(state="failed", replayed=True)))
            exchange, retire = transport.exchange, transport.retire
            entered, release = asyncio.Event(), asyncio.Event()
            cleanup = ExchangeFailed("Retirement failed", primary=OSError(errno.EIO, "PRIVATE_CLEANUP"))

            async def held_exchange(request, *, maximum_bytes, owner=None):
                try:
                    return await exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                except ExchangeFailed:
                    transport.retirement_confirmed, transport.pending_owner = False, owner
                    raise

            async def held_retirement(*, owner=None):
                entered.set()
                await release.wait()
                if late_evidence == "cleanup":
                    raise cleanup
                if late_evidence == "certificate":
                    lost.primary = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
                await retire(owner=owner)

            transport.exchange, transport.retire = held_exchange, held_retirement
            claims, retry = AttemptClaims(), Mock()
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                claims,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=StartRecovery(0.001, retry, Mock()),
            )
            task = asyncio.create_task(controller.run(record()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(len(transport.calls), 1)
                self.assertFalse(task.done())
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
                await asyncio.sleep(0.02)
                release.set()
                if late_evidence == "availability":
                    result = await task
                    self.assertIsInstance(result, AcknowledgedAttempt)
                    retry.assert_called_once()
                    self.assertEqual(len(transport.calls), 2)
                    self.assertEqual(await store.inventory(), ())
                else:
                    with self.assertRaises(ControllerFailed) as caught:
                        await task
                    failure = caught.exception.cause
                    self.assertIsInstance(failure, StartFailed)
                    if late_evidence == "cleanup":
                        self.assertIn(cleanup, failure.settlement_failures)
                    retry.assert_not_called()
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(await store.inventory(), (record(),))
            finally:
                release.set()
                await cancel_task(task)
                await store.close()

    async def test_policy_rejects_nonfinite_bounds_and_missing_observers(self):
        for value in (True, 0, -1, float("nan"), float("inf"), "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    StartRecovery(value, Mock(), Mock())
        with self.assertRaises(ValueError):
            StartRecovery(1, None, Mock())
