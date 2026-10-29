"""File failures spend shared budgets and never hand unverified bytes to readers."""

import asyncio
import errno
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.uploads import UploadFailed, UploadInputRejected
from tests.fakes.acquisition import file_sources, observe_source_files, private_source_directory
from tests.fakes.uploads import UPLOAD_REF


class UploadFileAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_create_settles_once_without_issuing_a_late_grant(self):
        await self._held_create(cancel=True, late_failure=None)

    async def test_expired_create_settles_after_source_deadline_without_issuing_a_grant(self):
        await self._held_create(cancel=False, late_failure=None)

    async def test_late_create_failure_keeps_intrinsic_cause_and_original_cancellation(self):
        await self._held_create(cancel=True, late_failure=OSError(errno.EIO, "PRIVATE_CREATE"))

    async def test_late_metadata_failure_after_open_closes_and_removes_the_original_file(self):
        owner, api, _, _ = file_sources()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        failure = OSError(errno.EIO, "PRIVATE_MODE_CHANGE")
        await owner.inspect()

        def held_mode_change(_descriptor, _mode):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("Test did not release its original file mode change")
            raise failure

        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.uploads.os.fchmod", side_effect=held_mode_change),
        ):
            task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.05))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(len(calls.names), 1)
                self.assertEqual(len(list(Path(root).iterdir())), 1)
                task.cancel("creation interrupted after open")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                retained = owner.last_failure
                self.assertIs(caught.exception.__cause__, retained)
                self.assertEqual(calls.closed, [])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await owner.settle_file()
            self.assertIs(retained.cause.cause, failure)
            self.assertIs(retained.interruption, caught.exception)
            self.assertEqual(len(calls.closed), 1)
            self.assertEqual(len(calls.removed), 1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls, ["membership"])

    async def test_expired_create_can_finish_cleanup_within_grace_without_claiming_pending_io(self):
        owner, api, _, _ = file_sources(seconds=0.1)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with private_source_directory() as (root, directory), observe_source_files(directory) as calls:

            def held_open(name, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("Test did not release original file creation during cleanup")
                return calls.open(name, *args, **kwargs)

            with patch("provider_core.uploads.os.open", side_effect=held_open):
                task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=1))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    async with asyncio.timeout(1):
                        while owner.last_failure is None:
                            await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(UploadFailed) as caught:
                        await task
                    self.assertIn("exceeded its allotted time", str(caught.exception))
                    self.assertNotIn("remains owned", str(caught.exception))
                    self.assertEqual(len(calls.closed), 1)
                    self.assertEqual(len(calls.removed), 1)
                    self.assertEqual(list(Path(root).iterdir()), [])
                    self.assertEqual(api.calls, ["membership"])
                    with self.assertRaisesRegex(UploadFailed, "total deadline expired"):
                        await owner.inspect()
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def _held_create(self, *, cancel, late_failure):
        # silent-defaults: allow-contract -- This fault scenario deliberately separates cancellation from deadline expiry.
        owner, api, store, _ = file_sources(seconds=2 if cancel else 0.1)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with private_source_directory() as (root, directory), observe_source_files(directory) as calls:

            def held_open(name, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("Test did not release its original file creation")
                if late_failure is not None:
                    raise late_failure
                return calls.open(name, *args, **kwargs)

            with patch("provider_core.uploads.os.open", side_effect=held_open) as create:
                task = asyncio.create_task(
                    owner.acquire_file(
                        UPLOAD_REF,
                        directory_fd=directory,
                        retirement_seconds=0.05,
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if cancel:
                        task.cancel("original caller interruption")
                    with self.assertRaises(asyncio.CancelledError if cancel else UploadFailed) as caught:
                        await task
                    retained = owner.last_failure
                    self.assertFalse(retained.grant_issued)
                    self.assertEqual(api.calls, ["membership"])
                    self.assertTrue(store.retirement_confirmed)
                    self.assertEqual(calls.closed, [])
                    self.assertEqual(calls.removed, [])
                    with self.assertRaises(UploadFailed):
                        await owner.inspect()
                    self.assertIs(owner.last_failure, retained)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await owner.settle_file()
                self.assertEqual(create.call_count, 1)
                self.assertEqual(api.calls, ["membership"])
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertIs(owner.last_failure, retained)
                if late_failure is None:
                    self.assertEqual(len(calls.closed), 1)
                    self.assertEqual(len(calls.removed), 1)
                else:
                    self.assertIs(retained.cause.cause, late_failure)
                    self.assertIs(retained.interruption, caught.exception)
                    self.assertEqual(calls.closed, [])

    async def test_repeated_cancellation_during_create_cleanup_keeps_original_exception_and_evidence(self):
        owner, api, _, _ = file_sources()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with private_source_directory() as (root, directory), observe_source_files(directory) as calls:

            def held_open(name, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("Test did not release original file creation")
                return calls.open(name, *args, **kwargs)

            with patch("provider_core.uploads.os.open", side_effect=held_open):
                task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=1))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel("first cancellation")
                    async with asyncio.timeout(1):
                        while owner.last_failure is None:
                            await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    first = owner.last_failure.cause
                    task.cancel("cleanup cancellation")
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertIs(caught.exception, first)
                    self.assertIs(caught.exception.__cause__, owner.last_failure)
                    self.assertEqual(api.calls, ["membership"])
                    self.assertEqual(calls.closed, [])
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await owner.settle_file()
                self.assertEqual(list(Path(root).iterdir()), [])

    async def test_cancelled_final_close_never_hands_off_and_settles_once(self):
        await self._held_close(cancel=True, late_failure=None)

    async def test_expired_final_close_cannot_return_a_late_receipt(self):
        await self._held_close(cancel=False, late_failure=None)

    async def test_late_final_close_failure_remains_cleanup_evidence_and_is_never_retried(self):
        await self._held_close(cancel=True, late_failure=OSError(errno.EIO, "PRIVATE_CLOSE"))

    async def _held_close(self, *, cancel, late_failure):
        # silent-defaults: allow-contract -- This fault scenario deliberately separates cancellation from file-read expiry.
        owner, api, store, client = file_sources(seconds=2 if cancel else 0.1)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):

            def held_close(descriptor):
                if descriptor in calls.descriptors:
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(3):
                        raise TimeoutError("Test did not release its original file close")
                    calls.close(descriptor)
                    if late_failure is not None:
                        raise late_failure
                    return None
                return calls.close(descriptor)

            with patch("provider_core.uploads.os.close", side_effect=held_close):
                task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.05))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertTrue(store.retirement_confirmed)
                    self.assertTrue(client.closed)
                    if cancel:
                        task.cancel("original close interruption")
                        async with asyncio.timeout(1):
                            while owner.last_failure is None:
                                await asyncio.sleep(0)
                        original = owner.last_failure.cause
                        self.assertFalse(task.done())
                        task.cancel("later cleanup interruption")
                    with self.assertRaises(asyncio.CancelledError if cancel else UploadFailed) as caught:
                        await task
                    if cancel:
                        self.assertIs(caught.exception, original)
                        self.assertIs(caught.exception.__cause__, owner.last_failure)
                    self.assertEqual(calls.closed, [])
                    self.assertEqual(calls.removed, [])
                    self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
                    self.assertTrue(owner.last_failure.grant_issued)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if late_failure is None:
                        await owner.settle_file()
                    else:
                        with self.assertRaises(UploadFailed):
                            await owner.settle_file()
                self.assertEqual(len(calls.closed), 1)
                self.assertEqual(api.calls.count("grant"), 1)
                if late_failure is None:
                    self.assertEqual(len(calls.removed), 1)
                    self.assertEqual(list(Path(root).iterdir()), [])
                else:
                    retained = owner.last_failure
                    self.assertIs(retained.cleanup_failures[0].cause, late_failure)
                    with self.assertRaises(UploadFailed):
                        await owner.settle_file()
                    self.assertIs(owner.last_failure, retained)
                    self.assertEqual(len(calls.closed), 1)
                    self.assertEqual(calls.removed, [])

    async def test_failed_close_does_not_close_a_reused_descriptor_number(self):
        owner, _, _, client = file_sources()
        close_failure = OSError(errno.EIO, "PRIVATE_CLOSE")
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):

            def uncertain_close(descriptor):
                calls.close(descriptor)
                if descriptor in calls.descriptors:
                    raise close_failure

            with patch("provider_core.uploads.os.close", side_effect=uncertain_close):
                with self.assertRaises(UploadFailed) as caught:
                    await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.05)
                retained = owner.last_failure
                self.assertIs(caught.exception.cause, close_failure)
                self.assertEqual(len(calls.closed), 1)
                descriptor = calls.descriptors[0]
                replacement = os.open(Path(root, "replacement"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                if replacement != descriptor:
                    os.dup2(replacement, descriptor)
                try:
                    with self.assertRaises(UploadFailed):
                        await owner.settle_file()
                    self.assertIs(owner.last_failure, retained)
                    self.assertEqual(len(calls.closed), 1)
                    self.assertEqual(calls.removed, [])
                    os.write(descriptor, b"still belongs to the replacement")
                    self.assertEqual(Path(root, "replacement").read_bytes(), b"still belongs to the replacement")
                finally:
                    calls._close(descriptor)
                    if replacement != descriptor:
                        calls._close(replacement)

    async def test_cancelled_remove_keeps_original_verification_failure_and_observes_one_unlink(self):
        owner, api, _, client = file_sources()
        client.headers = []
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):

            def held_remove(name, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("Test did not release its original file removal")
                return calls.unlink(name, *args, **kwargs)

            with patch("provider_core.uploads.os.unlink", side_effect=held_remove) as unlink:
                task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=1))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    original = owner.last_failure
                    self.assertIn("content-type", str(original))
                    task.cancel("cleanup removal interrupted")
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertIs(caught.exception.__cause__, original)
                    self.assertEqual(len(calls.closed), 1)
                    self.assertEqual(calls.removed, [])
                    self.assertEqual(len(list(Path(root).iterdir())), 1)
                    with self.assertRaises(UploadFailed):
                        await owner.inspect()
                    self.assertIs(owner.last_failure, original)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await owner.settle_file()
                self.assertEqual(unlink.call_count, 1)
                self.assertEqual(len(calls.removed), 1)
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertEqual(api.calls.count("grant"), 1)

    async def test_pending_store_write_blocks_file_cleanup_until_exact_transport_retirement(self):
        await self._held_store_write(late_failure=None)

    async def test_late_store_write_failure_is_reachable_from_the_original_cancellation(self):
        await self._held_store_write(late_failure=OSError(errno.ENOSPC, "PRIVATE_STORE_WRITE"))

    async def _held_store_write(self, *, late_failure):
        owner, api, store, client = file_sources()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        write = os.write
        await owner.inspect()

        def held_write(descriptor, data):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("Test did not release its original store write")
            if late_failure is not None:
                raise late_failure
            return write(descriptor, data)

        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            patch("provider_core.transport.os.write", side_effect=held_write) as writing,
        ):
            task = asyncio.create_task(owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel("store transfer interrupted")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                retained = owner.last_failure
                self.assertIs(caught.exception.__cause__, retained)
                self.assertFalse(store.retirement_confirmed)
                self.assertIs(store.pending_owner, owner)
                with self.assertRaises(UploadFailed):
                    await owner.settle_file()
                self.assertIs(owner.last_failure, retained)
                self.assertEqual(calls.closed, [])
                self.assertEqual(calls.removed, [])
                self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.retire(owner=owner)
                await owner.settle_file()
            self.assertTrue(store.retirement_confirmed)
            self.assertEqual(len(calls.closed), 1)
            self.assertEqual(len(calls.removed), 1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(writing.call_count, 1)
            self.assertEqual(api.calls.count("grant"), 1)
            if late_failure is not None:
                self.assertIs(retained.cause.primary, late_failure)

    async def test_replacement_file_is_never_removed_by_failed_acquisition_cleanup(self):
        await self._replacement_entry(symlink=False)

    async def test_replacement_symlink_is_not_followed_or_removed(self):
        await self._replacement_entry(symlink=True)

    async def _replacement_entry(self, *, symlink):
        owner, api, _, client = file_sources()
        client.headers = []
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            with (
                patch("provider_core.uploads.os.unlink", side_effect=OSError(errno.EACCES, "PRIVATE_REMOVE")),
                self.assertRaises(UploadFailed),
            ):
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            retained = owner.last_failure
            self.assertEqual(len(calls.closed), 1)
            source = Path(root, calls.names[0])
            replacement = Path(root, "replacement")
            replacement.write_bytes(b"replacement must survive")
            if symlink:
                source.unlink()
                source.symlink_to(replacement.name)
            else:
                replacement.replace(source)
            with self.assertRaises(UploadFailed):
                await owner.settle_file()
            self.assertIs(owner.last_failure, retained)
            self.assertEqual(source.read_bytes(), b"replacement must survive")
            self.assertEqual(source.is_symlink(), symlink)
            self.assertEqual(api.calls.count("grant"), 1)
            source.unlink()
            await owner.settle_file()
            if symlink:
                self.assertEqual(replacement.read_bytes(), b"replacement must survive")

    async def test_unknown_created_file_identity_is_not_reported_as_a_replacement(self):
        owner, api, _, _ = file_sources()
        await owner.inspect()
        fstat = os.fstat
        failure = OSError(errno.EIO, "PRIVATE_IDENTITY")
        with private_source_directory() as (root, directory), observe_source_files(directory) as calls:

            def inspect(descriptor):
                if descriptor in calls.descriptors:
                    raise failure
                return fstat(descriptor)

            with patch("provider_core.uploads.os.fstat", side_effect=inspect), self.assertRaises(UploadFailed) as caught:
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception.cause, failure)
            cleanup = owner.last_failure.cleanup_failures[0].cause
            self.assertIn("identity could not be confirmed", str(cleanup))
            self.assertNotIn("replacement", str(cleanup))
            self.assertEqual(api.calls, ["membership"])
            self.assertEqual(len(calls.closed), 1)
            self.assertEqual(calls.removed, [])
            Path(root, calls.names[0]).unlink()
            await owner.settle_file()

    async def test_polling_pending_removal_does_not_erase_a_concrete_cleanup_cause(self):
        owner, _, _, client = file_sources()
        client.headers = []
        failure = OSError(errno.EACCES, "PRIVATE_REMOVE")
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            observe_source_files(directory) as calls,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            with patch("provider_core.uploads.os.unlink", side_effect=failure), self.assertRaises(UploadFailed):
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.05)
            retained = owner.last_failure
            concrete = retained.cleanup_failures[0]
            self.assertIs(concrete.cause, failure)

            def held_remove(name, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise TimeoutError("Test did not release its original file removal")
                return calls.unlink(name, *args, **kwargs)

            with patch("provider_core.uploads.os.unlink", side_effect=held_remove) as unlink:
                task = asyncio.create_task(owner.settle_file())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    with self.assertRaises(UploadFailed):
                        await task
                    with self.assertRaises(UploadFailed):
                        await owner.settle_file()
                    self.assertIs(owner.last_failure, retained)
                    self.assertEqual(retained.cleanup_failures, (concrete,))
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await owner.settle_file()
                self.assertEqual(unlink.call_count, 1)
                self.assertEqual(len(calls.removed), 1)
                self.assertEqual(list(Path(root).iterdir()), [])

    async def test_nonprivate_destination_refuses_before_refresh_or_grant(self):
        owner, api, _, _ = file_sources()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool") as client,
        ):
            os.fchmod(directory, 0o755)
            with self.assertRaises(UploadFailed):
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls, ["membership"])
            client.assert_not_called()
            os.fchmod(directory, 0o700)
            await owner.inspect()

    async def test_expiry_after_successful_final_close_still_refuses_and_removes_file(self):
        with patch("provider_core.uploads.time") as clock:
            clock.monotonic.return_value = 100
            owner, api, _, client = file_sources()
            await owner.inspect()
            with (
                private_source_directory() as (root, directory),
                observe_source_files(directory) as calls,
                patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            ):

                def close_at_expiry(descriptor):
                    calls.close(descriptor)
                    clock.monotonic.return_value = 161

                with (
                    patch("provider_core.uploads.os.close", side_effect=close_at_expiry),
                    self.assertRaisesRegex(UploadFailed, "total deadline expired"),
                ):
                    await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertEqual(len(calls.closed), 1)
                self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
                self.assertEqual(api.calls.count("grant"), 1)

    async def test_short_and_oversized_bodies_never_hand_off_files_and_keep_actual_byte_charges(self):
        for body in (b"short", b"oversized"):
            owner, api, _, client = file_sources()
            client.stream.chunks = [body]
            await owner.inspect()
            with (
                self.subTest(size=len(body)),
                private_source_directory() as (root, directory),
                patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            ):
                with self.assertRaises(UploadFailed):
                    await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertEqual(owner.received_bytes, len(body))
                self.assertEqual(api.calls.count("grant"), 1)

    async def test_fixed_file_limit_remains_correctable_without_creating_file_or_issuing_grant(self):
        owner, api, _, _ = file_sources(source_bytes=7)
        await owner.inspect()
        with private_source_directory() as (root, directory):
            with self.assertRaises(UploadInputRejected):
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls, ["membership"])

    async def test_failed_file_transfer_spends_the_byte_path_budget_and_removes_unverified_file(self):
        owner, api, _, client = file_sources(total_bytes=8)
        client.stream.chunks = [b"tampered"]
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as clients,
        ):
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertTrue(caught.exception.grant_issued)
            self.assertEqual(owner.received_bytes, 8)
            self.assertEqual(list(Path(root).iterdir()), [])
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire(UPLOAD_REF)
            self.assertNotIsInstance(caught.exception, UploadInputRejected)
            self.assertEqual(api.calls.count("grant"), 1)
            self.assertEqual(clients.call_count, 1)

    async def test_same_length_corruption_never_returns_file_and_keeps_consumed_charge(self):
        owner, _, _, client = file_sources()
        client.stream.chunks = [b"tampered"]
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            with self.assertRaisesRegex(UploadFailed, "SHA-256"):
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(owner.received_bytes, 8)

    async def test_failed_byte_transfer_cannot_regain_budget_by_switching_to_file_output(self):
        owner, api, _, client = file_sources(total_bytes=8)
        client.stream.chunks = [b"tampered"]
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as clients,
        ):
            with self.assertRaises(UploadFailed):
                await owner.acquire(UPLOAD_REF)
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertNotIsInstance(caught.exception, UploadInputRejected)
            self.assertEqual(owner.received_bytes, 8)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls.count("grant"), 1)
            self.assertEqual(clients.call_count, 1)
