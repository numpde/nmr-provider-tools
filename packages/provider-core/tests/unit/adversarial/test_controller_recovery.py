"""Relay refusal, uncertain cleanup and cancellation never create another outcome."""

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from provider_core.checkpoint_store import CheckpointFailed
from provider_core.controller import AttemptController, ControllerFailed
from provider_core.delivery_client import DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull
from provider_core.execution import ExecutionFailed
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import TerminalSelectedCheckpoint
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, api
from tests.fakes.attempt_store import record, scope
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.controller import SingleRetirementSession, cancel_task, empty_store, lifecycle_replies


class ControllerRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_refusal_or_unsettled_cleanup_never_retries(self):
        for failure in (DeliveryRejected("Wrong relay acknowledgement"),
                        DeliveryUnavailable("Local policy refused", status=403)):
            with self.subTest(kind=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                store = await empty_store(directory, maximum_records=1)
                session, delivery, retry = SingleRetirementSession(store), RecordingDelivery(), Mock()
                delivery.submit = AsyncMock(side_effect=failure)
                controller = AttemptController(api(Transport(*lifecycle_replies()), scope().api_origin), store, AttemptClaims(), session,
                    delivery=delivery, on_delivery_retry=retry, delivery_retry_initial_seconds=0.001,
                    delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
                try:
                    with self.assertRaises(ControllerFailed) as caught:
                        await controller.run(record())
                    self.assertIsInstance(caught.exception.cause, ExecutionFailed)
                    self.assertIs(caught.exception.cause.cause, failure)
                    delivery.submit.assert_awaited_once()
                    retry.assert_not_called()
                    self.assertIs(type((await store.inventory())[0]), TerminalSelectedCheckpoint)
                    self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                finally:
                    await store.close()

    async def test_failed_retry_recipient_keeps_original_send_evidence_without_new_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, delivery = SingleRetirementSession(store), RecordingDelivery()
            original, output_failure = DeliveryFull("Relay queue full"), OSError("Authored recipient failure")
            delivery.submit = AsyncMock(side_effect=original)
            controller = AttemptController(api(Transport(*lifecycle_replies()), scope().api_origin), store, AttemptClaims(), session,
                delivery=delivery, on_delivery_retry=Mock(side_effect=output_failure), delivery_retry_initial_seconds=0.001,
                delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as caught:
                    await controller.run(record())
                self.assertIs(caught.exception.cause.cause, output_failure)
                self.assertIs(output_failure.__context__, original)
                delivery.submit.assert_awaited_once()
                self.assertIs(type((await store.inventory())[0]), TerminalSelectedCheckpoint)
            finally:
                await store.close()

    async def test_accepted_metadata_failure_drops_payload_without_replaying_successful_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, delivery, retry = SingleRetirementSession(store), RecordingDelivery(), Mock()
            original_submit = delivery.submit
            delivery.submit = AsyncMock(side_effect=original_submit)
            failure = CheckpointFailed("Cannot retain accepted metadata")
            controller = AttemptController(api(Transport(*lifecycle_replies()), scope().api_origin), store, AttemptClaims(), session,
                delivery=delivery, on_delivery_retry=retry, delivery_retry_initial_seconds=0.001,
                delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with patch.object(store._database, "accept_delivery", side_effect=failure):
                    with self.assertRaises(ControllerFailed) as caught:
                        await controller.run(record())
                self.assertIs(caught.exception.cause.cause, failure)
                self.assertIsNone(caught.exception.cause.outcome)
                self.assertIsNotNone(caught.exception.cause.delivery_acceptance)
                self.assertIsNone(controller.operation._delivery_outcome)
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                delivery.submit.assert_awaited_once()
                retry.assert_not_called()
                self.assertIs(type((await store.inventory())[0]), TerminalSelectedCheckpoint)
            finally:
                await store.close()

    async def test_caller_cancellation_stops_backoff_and_settle_never_creates_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, delivery, entered = SingleRetirementSession(store), RecordingDelivery(), asyncio.Event()
            delivery.submit = AsyncMock(side_effect=DeliveryFull("Relay full"))
            controller = AttemptController(api(Transport(*lifecycle_replies()), scope().api_origin), store, AttemptClaims(), session,
                delivery=delivery, on_delivery_retry=lambda *_: entered.set(), delivery_retry_initial_seconds=10,
                delivery_retry_max_seconds=10, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            task = asyncio.create_task(controller.run(record()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                delivery.submit.assert_awaited_once()
                self.assertIs(type((await store.inventory())[0]), TerminalSelectedCheckpoint)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            finally:
                await cancel_task(task)
                await store.close()

    async def test_original_ipc_retirement_and_late_permission_precede_retry(self):
        for disposition in ("permanent", "cleanup", "available"):
            with self.subTest(disposition=disposition), tempfile.TemporaryDirectory() as directory:
                store = await empty_store(directory, maximum_records=1)
                session, delivery = SingleRetirementSession(store), RecordingDelivery()
                started, release = asyncio.Event(), asyncio.Event()
                attempted, submit = [], delivery.submit
                failure = DeliveryUnavailable("Original local transport unfinished")
                async def interrupted(item):
                    attempted.append(item.body)
                    if len(attempted) == 1:
                        delivery.retirement_confirmed = False
                        raise failure
                    return await submit(item)
                delivery.submit = interrupted
                async def retire():
                    started.set()
                    await release.wait()
                    if disposition == "cleanup":
                        raise ExchangeFailed("Original IPC cleanup is unconfirmed", primary=OSError())
                    delivery.retirement_confirmed = True
                delivery.retire = retire
                delivery.can_retry = Mock(side_effect=lambda _: disposition == "available")
                retry = Mock()
                controller = AttemptController(api(Transport(*lifecycle_replies()), scope().api_origin), store, AttemptClaims(), session,
                    delivery=delivery, on_delivery_retry=retry, delivery_retry_initial_seconds=0.001,
                    delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
                task = asyncio.create_task(controller.run(record()))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    self.assertEqual(len(attempted), 1)
                    self.assertFalse(task.done())
                    delivery.can_retry.assert_not_called()
                    retry.assert_not_called()
                    release.set()
                    if disposition == "available":
                        await task
                        self.assertEqual(attempted, [delivery.items[0].body] * 2)
                        retry.assert_called_once()
                        self.assertEqual(await store.inventory(), ())
                    else:
                        with self.assertRaises(ControllerFailed):
                            await task
                        self.assertEqual(len(attempted), 1)
                        retry.assert_not_called()
                        self.assertIs(type((await store.inventory())[0]), TerminalSelectedCheckpoint)
                    self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                finally:
                    release.set()
                    await cancel_task(task)
                    await store.close()

    async def test_retry_settings_reject_nonfinite_bounds_and_missing_recipient_before_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, transport = SingleRetirementSession(store), Transport()
            settings = dict(delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=1,
                delivery_retry_max_seconds=2, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                for key, value in (("delivery_retry_initial_seconds", True), ("delivery_retry_initial_seconds", 0),
                                   ("delivery_retry_max_seconds", float("nan")), ("delivery_retry_max_seconds", float("inf")),
                                   ("delivery_retry_max_seconds", 0.5), ("on_delivery_retry", None)):
                    with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                        AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session, **(settings | {key: value}))
                self.assertEqual(session.events, [])
                self.assertEqual(transport.calls, [])
            finally:
                await store.close()
