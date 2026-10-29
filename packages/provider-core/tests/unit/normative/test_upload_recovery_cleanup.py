"""Independent API, store and retained-file retirement keep their actual owners."""

import asyncio
import errno
import os
import ssl
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpcore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.task_boundary import contain_task_exit
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadFailed, UploadLimits, UploadRecovery
from tests.fakes.acquisition import private_source_directory, store_headers
from tests.fakes.api import response_headers
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF
from tests.unit.normative.test_upload_recovery import api_reply, store_reply, refusal, MEMBERSHIP, GRANT
from tests.unit.normative.test_upload_transfer_retry import temporary_read_error


class UploadRecoveryCleanupTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary cleanup fixtures have one second; the retained-API witness supplies its shorter actual budget.
    def owner(self, *, seconds=1, api_transport=None):
        api_transport = api_transport or HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1,
                                                      retirement_seconds=0.03)
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.03)
        api = ProviderAPI(SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                          api_transport, expected_topology="dev-local")
        notices = []
        recovery = UploadRecovery(3, 0.001, 0.001, asyncio.Event(), lambda *facts: notices.append(facts))
        owner = JobUploads("job:example", api=api, store=store, store_origin="https://store.example",
                           limits=UploadLimits(8, 100, 8, seconds), recovery=recovery)
        return owner, api, store, notices

    async def test_unresolved_api_and_pending_file_are_retired_independently(self):
        api_release, file_release = asyncio.Event(), threading.Event()
        original = httpcore.ReadTimeout("protected late grant failure")
        class LateGrant(Stream):
            async def __aiter__(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await api_release.wait()
                    raise original
                yield b"unreachable"
        api_transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.02)
        owner, api, store, notices = self.owner(seconds=0.15, api_transport=api_transport)
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), Client(LateGrant([]), headers=response_headers())]
        api_retire, store_retire, close = api.retire, store.retire, os.close
        calls = []
        def held_close(fd):
            if not file_release.wait(2):
                raise TimeoutError("test did not release original private close")
            return close(fd)
        async def retire_api(*, owner=None):
            self.assertFalse(api.retirement_confirmed)
            file_release.set()
            await api_retire(owner=owner)
        async def retire_store(*, owner=None):
            calls.append(owner)
            await store_retire(owner=owner)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients
        ), patch("provider_core.uploads.os.close", side_effect=held_close):
            try:
                await owner.inspect()
                with patch.object(api, "retire", side_effect=retire_api), patch.object(
                    store, "retire", side_effect=retire_store
                ):
                    with self.assertRaises(UploadFailed) as caught:
                        await asyncio.wait_for(owner.acquire_file(UPLOAD_REF, directory_fd=directory,
                                                                  retirement_seconds=0.02), 1)
                self.assertEqual(calls, [owner])
                self.assertFalse(api.retirement_confirmed)
                self.assertFalse(owner._transfer_owner.file_cleanup_pending)
                self.assertEqual(list(Path(root).iterdir()), [])
                self.assertIs(caught.exception, owner.last_failure)
                self.assertIn(api_transport.last_failure, caught.exception.cleanup_failures)
                self.assertEqual(notices, [])
                self.assertEqual(owner.requests_started, 3)
            finally:
                file_release.set()
                api_release.set()
                await api_retire()
                await store_retire(owner=owner)
                if owner._transfer_owner.file_cleanup_pending:
                    await owner.settle_file()

    async def test_cleanup_cancellation_and_fatal_groups_do_not_skip_other_retirement(self):
        for cancelled, granted in ((True, True), (False, True), (False, False)):
            with self.subTest(cancelled=cancelled, granted=granted), private_source_directory() as (root, directory):
                owner, api, store, notices = self.owner()
                clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT),
                           store_reply(failure=temporary_read_error())]
                if not granted:
                    clients = [api_reply(MEMBERSHIP), refusal(503, "service_unavailable")]
                prior = RuntimeError("protected pre-existing cancellation cause")
                interruption = asyncio.CancelledError("original cleanup cancellation")
                interruption.__cause__ = prior
                api_fatal = SystemExit("protected API fatal")
                store_fatal = BaseExceptionGroup("protected store fatal", (KeyboardInterrupt(), SystemExit()))
                unlink, blocked, calls = os.unlink, [True], []
                store_retire, api_retire = store.retire, api.retire
                def remove(*args, **kwargs):
                    if blocked[0]:
                        raise OSError(errno.EACCES, "protected original removal failure")
                    return unlink(*args, **kwargs)
                async def fail_api(*, owner=None):
                    blocked[0] = False
                    if cancelled:
                        raise interruption
                    raise api_fatal
                async def fail_store(*, owner=None):
                    calls.append(owner)
                    await store_retire(owner=owner)
                    raise store_fatal
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients), patch(
                    "provider_core.uploads.os.unlink", side_effect=remove
                ):
                    await owner.inspect()
                    try:
                        with patch.object(api, "retire", side_effect=fail_api), patch.object(
                            store, "retire", side_effect=fail_store
                        ):
                            task = asyncio.create_task(contain_task_exit(owner.acquire_file, UPLOAD_REF,
                                                                         directory_fd=directory, retirement_seconds=0.1))
                            expected = asyncio.CancelledError if cancelled else BaseExceptionGroup
                            with self.assertRaises(expected) as caught:
                                await task
                        self.assertEqual(calls, [owner])
                        self.assertEqual(list(Path(root).iterdir()), [])
                        self.assertFalse(owner._transfer_owner.file_cleanup_pending)
                        record = owner.last_failure
                        self.assertIsInstance(record, UploadFailed)
                        if granted:
                            self.assertIs(record.cause, owner.recovery_failure)
                        else:
                            self.assertIs(owner.recovery_failure, api.last_failure)
                            self.assertFalse(record.grant_issued)
                        self.assertIn(store_fatal, record.cleanup_failures)
                        if cancelled:
                            self.assertIs(caught.exception, interruption)
                            self.assertIs(interruption.__cause__, record)
                            self.assertIs(record.interruption, prior)
                            self.assertNotIn(interruption, record.cleanup_failures)
                        else:
                            first_group = record.cleanup_failures[0]
                            self.assertIs(first_group.exceptions[0], api_fatal)
                            self.assertEqual(caught.exception.exceptions, (first_group, store_fatal, record))
                        self.assertEqual(notices, [])
                        expected_requests = 2
                        if granted:
                            expected_requests = 4
                        self.assertEqual(owner.requests_started, expected_requests)
                    finally:
                        blocked[0] = False
                        await api_retire()
                        await store_retire(owner=owner)
                        if owner._transfer_owner.file_cleanup_pending:
                            await owner.settle_file()
