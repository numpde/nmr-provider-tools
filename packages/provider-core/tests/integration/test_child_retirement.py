"""Distinguish cancelled tasks/closed IPC from an exact scripted child stopping.

This qualifies the core session seam, not a sibling's scientific worker/protocol,
process tree, source selection, native parser, warm reuse or production isolation.
"""

from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint
from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
import asyncio
import base64
import hashlib
import itertools
import json
import tempfile
import unittest
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from provider_core.attempt_record import LocalExecutionPhase, TerminalOperation
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused
from provider_core.ownership import OwnershipRefused
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.child_session import ChildChannelClosed, ChildSession, ChildStopUnconfirmed
from tests.fakes.execution import acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api
from tests.fakes.transport import Client, Stream
from tests.fakes.upload_child_session import UploadChildSession
from tests.fakes.uploads import UPLOAD_REF


class ChildRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquired_source_survives_closed_reader_output_until_exact_child_stop_and_settlement(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session, source, source_api = await self._source_resources(cleanup, "close_output")
            executor = self._executor(store, claim, session)
            with self.assertRaises(ExecutionFailed) as failed:
                await self._run(cleanup, executor, acknowledged)
            self.assertIsInstance(failed.exception.cause, ChildChannelClosed)
            process = session.child.process
            self.assertIsNone(process.returncode)
            self.assertEqual(session.source_reply["content_hash"], session.source.content_hash)
            self.assertEqual(session.source_reply["marker"], "acquired-source-observed")
            self.assertTrue(source.exists())
            with self.assertRaises(OwnershipRefused):
                claim.release()
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            self.assertTrue(source.exists())
            self.assertEqual(session.source_removals, 0)
            await session.child.stop()
            self.assertTrue(source.exists(), "Stopping the child does not settle composition's file cleanup")
            unlink_failure = OSError("Authored source removal failure")
            with (
                patch("tests.fakes.upload_child_session.os.unlink", side_effect=unlink_failure) as remove,
                self.assertRaises(ExecutionFailed) as cleanup_failed,
            ):
                await executor.settle()
            self.assertIn(unlink_failure, cleanup_failed.exception.cleanup_failures)
            remove.assert_called_once_with(session.source.name, dir_fd=session.directory_fd)
            self.assertTrue(source.exists())
            with self.assertRaises(OwnershipRefused):
                claim.release()
            await executor.settle()
            self.assertIs(session.child.process, process)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertFalse(source.exists())
            self.assertEqual(session.source_removals, 1)
            with self.assertRaises(ExecutionRefused):
                await executor.settle()
            self.assertEqual(session.source_removals, 1)
            self.assertEqual(session.generations, 1)
            self.assertEqual(source_api.calls.count("grant"), 1)
            self.assertIs(type((await store.inventory())[0]), EnteredCheckpoint)
            claim.release()

    async def test_cancelled_reader_work_reaps_the_same_child_before_releasing_its_acquired_source(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session, source, source_api = await self._source_resources(cleanup, "block_read")
            executor = self._executor(store, claim, session)
            task = asyncio.create_task(executor.run(acknowledged))
            cleanup.push_async_callback(self._cancel_executor, task)
            await asyncio.wait_for(session.reading_started.wait(), 2)
            process = session.child.process
            self.assertIsNone(process.returncode)
            self.assertTrue(source.exists())
            self.assertEqual(session.source_read_reply, {"state": "reading", "byte_length": session.source.byte_length})
            self.assertIsNone(session.source_reply)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self._finished(task)
            self.assertIs(session.child.process, process)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertTrue(session.source_cleanup_observed_stopped_child)
            self.assertFalse(source.exists())
            self.assertEqual(session.source_removals, 1)
            self.assertEqual(session.generations, 0)
            self.assertEqual(source_api.calls.count("grant"), 1)
            self.assertIsNone(executor.last_failure.outcome)
            self.assertIs(type((await store.inventory())[0]), AcknowledgedCheckpoint)
            claim.release()

    async def test_prestart_source_rejection_cleans_its_owned_file_without_claiming_a_child_was_stopped(self):
        for mode in ("block", "close_output"):
            with self.subTest(mode=mode):
                async with AsyncExitStack() as cleanup:
                    store, acknowledged, claim, session, source, _ = await self._source_resources(cleanup, mode)
                    session.source = replace(session.source, job_ref="job:different")
                    executor = self._executor(store, claim, session, preparation_only=True)
                    with self.assertRaises(ExecutionFailed) as failed:
                        await self._run(cleanup, executor, acknowledged)
                    self.assertIsInstance(failed.exception.cause, ValueError)
                    self.assertIsNone(session.child.process)
                    self.assertFalse(session.source_cleanup_observed_stopped_child)
                    self.assertFalse(source.exists())
                    self.assertEqual(session.source_removals, 1)
                    self.assertEqual(session.generations, 0)
                    self.assertIs(type((await store.inventory())[0]), AcknowledgedCheckpoint)
                    claim.release()

    async def _source_resources(self, cleanup, mode):
        directory = cleanup.enter_context(tempfile.TemporaryDirectory())
        store, acknowledged, claim = await acknowledged_store(directory)
        cleanup.push_async_callback(store.close)
        root, descriptor = cleanup.enter_context(private_source_directory())
        acquisition, source_api, _, _ = file_sources()
        # Use a complete, independently authored marker; no scientific decoder
        # or model weights enter this source-lifetime join.
        body = b'{"marker":"acquired-source-observed"}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        source_api.member = replace(source_api.member, byte_length=len(body), content_hash=digest)
        source_api.grant = replace(source_api.grant, byte_length=len(body), content_hash=digest)
        client = Client(Stream([body]), headers=store_headers(body))
        await acquisition.inspect()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            receipt = await acquisition.acquire_file(UPLOAD_REF, directory_fd=descriptor, retirement_seconds=0.1)
        session = UploadChildSession(store, mode, receipt, directory_fd=descriptor)
        cleanup.push_async_callback(session.stop_child)
        return store, acknowledged, claim, session, Path(root, receipt.name), source_api

    async def test_ordered_success_retains_result_and_reaps_child_before_returning_publication(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "success")
            executor = self._executor(store, claim, session)
            terminal = await self._run(cleanup, executor, acknowledged)
            self.assertIs(type(terminal), DeliveryAcceptedCheckpoint)
            self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
            self.assertEqual(session.phases, [AcknowledgedCheckpoint, EnteredCheckpoint])
            self.assertEqual(session.generations, 1)
            self.assertTrue(session.child.pipes_closed)
            self.assertIsNotNone(session.child.process.returncode)
            body = json.loads(executor.delivery.items[0].body)
            result = json.loads(base64.b64decode(body["canonical_result_base64"], validate=True))
            self.assertEqual(
                result,
                {"state": "result", "source_fingerprint": acknowledged.start.input.input_fingerprint, "value": "scripted-only"},
            )
            self.assertEqual(await store.inventory(), (terminal,))
            claim.release()

    async def test_scripted_preparation_rejection_never_records_entry_or_generates(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "reject")
            executor = self._executor(store, claim, session, preparation_only=True)
            terminal = await self._run(cleanup, executor, acknowledged)
            self.assertIs(terminal.selection.operation, TerminalOperation.FAIL)
            self.assertIs(type(terminal.selection.attempt), AcknowledgedCheckpoint)
            self.assertEqual(session.generations, 0)
            self.assertIsNotNone(session.child.process.returncode)
            self.assertTrue(session.child.pipes_closed)
            claim.release()

    async def test_cancelled_generation_stops_exact_child_and_keeps_entered_history_without_publication(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "block")
            executor = self._executor(store, claim, session)
            task = asyncio.create_task(executor.run(acknowledged))
            cleanup.push_async_callback(self._cancel_executor, task)
            await asyncio.wait_for(session.generation_started.wait(), 2)
            original_process = session.child.process
            self.assertIsNone(original_process.returncode)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self._finished(task)
            self.assertIs(session.child.process, original_process)
            self.assertIsNotNone(original_process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertEqual(session.generations, 1)
            self.assertIsNone(executor.last_failure.outcome)
            self.assertIs(type((await store.inventory())[0]), EnteredCheckpoint)
            claim.release()

    async def test_closed_output_does_not_release_claim_until_that_same_child_is_stopped_and_reaped(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "close_output")
            executor = self._executor(store, claim, session)
            with self.assertRaises(ExecutionFailed) as failed:
                await self._run(cleanup, executor, acknowledged)
            original_process = session.child.process
            self.assertIsInstance(failed.exception.cause, ChildChannelClosed)
            self.assertIsNone(original_process.returncode)
            self.assertTrue(any(isinstance(cause, ChildStopUnconfirmed) for cause in failed.exception.cleanup_failures))
            with self.assertRaises(OwnershipRefused):
                claim.release()
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            self.assertIsNone(original_process.returncode)
            self.assertEqual(session.generations, 1)
            await session.child.stop()
            with self.assertRaises(OwnershipRefused):
                claim.release()
            self.assertIsNone(await executor.settle())
            self.assertIs(session.child.process, original_process)
            self.assertIsNotNone(original_process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertEqual(session.generations, 1)
            self.assertIs(type((await store.inventory())[0]), EnteredCheckpoint)
            claim.release()

    async def _resources(self, cleanup, mode):
        directory = cleanup.enter_context(tempfile.TemporaryDirectory())
        store, acknowledged, claim = await acknowledged_store(directory)
        cleanup.push_async_callback(store.close)
        session = ChildSession(store, mode)
        cleanup.push_async_callback(session.child.stop)
        return store, acknowledged, claim, session

    async def _cancel_executor(self, task):
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=1)
        if not task.done():
            raise TimeoutError("Test executor did not settle after cancellation; still attempt exact-child and store cleanup")
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self, cleanup, executor, acknowledged):
        task = asyncio.create_task(executor.run(acknowledged))
        cleanup.push_async_callback(self._cancel_executor, task)
        return await self._finished(task)

    async def _finished(self, task):
        # wait_for would await cancellation indefinitely if retirement regressed.
        # Retain the task for bounded cleanup even when this watchdog expires.
        await asyncio.wait((task,), timeout=3)
        if not task.done():
            raise TimeoutError("Test executor did not finish; cancel it and clean up its retained child")
        return task.result()

    # silent-defaults: allow-contract -- Normal fixtures reach running progress; refusal cases explicitly stop their reply sequence at preparation.
    def _executor(self, store, claim, session, *, preparation_only=False):
        beginning = (reply(observation()), reply(response()), reply(progress_response()), reply(observation()))
        # silent-defaults: allow-contract -- A pre-execution refusal must not receive a synthetic running acknowledgement.
        after = () if preparation_only else (reply(progress_response(phase="running")),)
        # Long poll interval isolates child/caller-cancellation correctness from
        # scheduler-dependent read counts; all waits have separate test watchdogs.
        transport = Transport(*itertools.chain(beginning, after, itertools.repeat(reply(observation()), 16)))
        return AttemptExecutor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.02, allowed_job_states=frozenset({"open"}),
            poll_seconds=10,
            task_settle_seconds=1,
        )
