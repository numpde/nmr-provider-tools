"""Logical source retries retain real transport provenance and spent budgets."""

import asyncio
import hashlib
import os
import random
import ssl
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpcore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadFailed, UploadInputRejected, UploadLimits, UploadRecovery, UploadRecoveryExhausted
from tests.fakes.acquisition import private_source_directory, store_headers
from tests.fakes.api import response_headers
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership
from tests.unit.normative.test_upload_transfer_retry import temporary_read_error


BODY = b"geometry"
HASH = "sha256:" + hashlib.sha256(BODY).hexdigest()
MEMBERSHIP = membership(uploads=[member(byte_length=len(BODY), content_hash=HASH)])
GRANT = grant(byte_length=len(BODY), content_hash=HASH, expires_at="2099-01-01T00:00:00Z")


def api_reply(document):
    return Client(Stream([encoded(document)]), headers=response_headers())


def refusal(status, code):
    body = {"type": "urn:nmr-api:problem:operation-conflict", "title": "Private title",
            "status": status, "instance": "urn:nmr-api:request:test", "request_id": "request:test",
            "code": code, "detail": "private detail"}
    return Client(Stream([encoded(body)]), status=status,
                  headers=response_headers(status=status, request_id=b"request:test"))


def store_reply(*, failure=None):
    chunks = [BODY]
    if failure is not None:
        chunks = [b"g"]
    return Client(Stream(chunks, failure=failure), headers=store_headers(BODY))


class UploadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- This fixture grants eight requests, a 100-byte total and two seconds unless a specific budget witness overrides them.
    def owner(self, *, recovery=None, requests=8, total=100, seconds=2, api_transport=None):
        api_transport = api_transport or HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1,
                                                      retirement_seconds=0.1)
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.1)
        api = ProviderAPI(SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                          api_transport, expected_topology="dev-local")
        return JobUploads("job:example", api=api, store=store, store_origin="https://store.example",
                          limits=UploadLimits(8, total, requests, seconds), recovery=recovery), api, store

    # silent-defaults: allow-contract -- Three attempts with a one-millisecond wait keep ordinary recovery fixtures bounded; pacing witnesses supply their actual policy.
    def policy(self, *, stopping=None, on_retry=None, attempts=3, initial=0.001):
        return UploadRecovery(attempts, initial, initial, stopping or asyncio.Event(),
                              on_retry or (lambda _failure, _seconds: None))

    async def test_one_shot_default_remains_one_attempt(self):
        owner, _, _ = self.owner()
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error())]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 4)
        self.assertEqual(owner.received_bytes, 1)
        self.assertIs(owner.recovery_failure, caught.exception)

    async def test_retry_verifies_same_source_after_exact_retirement_on_both_paths(self):
        for file in (False, True):
            with self.subTest(file=file), private_source_directory() as (root, directory):
                original = temporary_read_error()
                clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=original),
                           api_reply(MEMBERSHIP), api_reply(GRANT), store_reply()]
                notices, owners = [], []
                def notice(failure, seconds):
                    self.assertTrue(all(client.closed for client in clients[:4]))
                    self.assertEqual(list(Path(root).iterdir()), [])
                    notices.append(failure)
                owner, _, store = self.owner(recovery=self.policy(on_retry=notice))
                retire = store.retire
                async def observed_retire(*, owner=None):
                    owners.append(owner)
                    await retire(owner=owner)
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients), patch.object(
                    store, "retire", side_effect=observed_retire
                ):
                    await owner.inspect()
                    if file:
                        result = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
                        self.assertEqual(Path(root, result.name).read_bytes(), BODY)
                        os.unlink(result.name, dir_fd=directory)
                    else:
                        self.assertEqual((await owner.acquire(UPLOAD_REF)).data, BODY)
                expected_owner = None
                if file:
                    expected_owner = owner
                self.assertEqual(owners, [expected_owner])
                self.assertEqual(len(notices), 1)
                self.assertIs(owner.recovery_failure, notices[0])
                self.assertIs(owner.recovery_failure.cause.network_failure, original)
                self.assertTrue(owner.recovery_failure.grant_issued)
                self.assertIsNone(owner.last_failure)
                self.assertEqual(owner.requests_started, 7)
                self.assertEqual(owner.received_bytes, 9)
                self.assertTrue(all(client.closed for client in clients))
                with self.assertRaises(AttributeError):
                    owner.recovery_failure = None

    async def test_original_membership_must_match_before_new_grant(self):
        notices = []
        owner, _, _ = self.owner(recovery=self.policy(on_retry=lambda failure, _: notices.append(failure)))
        changed = membership(uploads=[member(description="changed", byte_length=8, content_hash=HASH)])
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error()),
                   api_reply(changed)]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaises(UploadInputRejected):
                await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 5)
        self.assertIs(owner.recovery_failure, notices[0])

    async def test_request_and_byte_budgets_are_never_refunded(self):
        owner, _, _ = self.owner(recovery=self.policy())
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error()),
                   api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error()), api_reply(MEMBERSHIP)]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadRecoveryExhausted, "3 requests.*1 remain"):
                await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 7)
        self.assertEqual(owner.received_bytes, 2)
        owner, _, _ = self.owner(recovery=self.policy(), total=8)
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error())]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadRecoveryExhausted, "remaining byte budget"):
                await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 4)
        self.assertEqual(owner.received_bytes, 1)

    async def test_attempt_cap_includes_initial_inspection(self):
        owner, _, _ = self.owner(recovery=self.policy(attempts=2))
        clients = [refusal(503, "service_unavailable"), refusal(503, "service_unavailable")]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            with self.assertRaises(UploadRecoveryExhausted):
                await owner.inspect()
        self.assertEqual(owner.requests_started, 2)
        self.assertIsInstance(owner.recovery_failure, ApiFailure)

    async def test_grant_diagnosis_and_undeclared_rate_status_never_retry(self):
        for status in (500, 429):
            with self.subTest(status=status):
                owner, _, _ = self.owner(recovery=self.policy())
                clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), refusal(status, "operation_conflict")]
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
                    await owner.inspect()
                    with self.assertRaises(ApiFailure) as caught:
                        await owner.acquire(UPLOAD_REF)
                self.assertEqual(owner.requests_started, 3)
                self.assertIs(owner.recovery_failure, caught.exception)
                self.assertTrue(caught.exception.issuance_may_have_committed)

    async def test_scientific_stop_and_notice_failure_prevent_another_request(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                stopping, notices = asyncio.Event(), []
                original_notice = RuntimeError("protected notice failure")
                def notice(failure, _seconds):
                    notices.append(failure)
                    if broken:
                        raise original_notice
                    stopping.set()
                owner, _, _ = self.owner(recovery=self.policy(stopping=stopping, on_retry=notice, initial=1))
                clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT),
                           store_reply(failure=temporary_read_error())]
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
                    await owner.inspect()
                    with self.assertRaises(UploadFailed) as caught:
                        await asyncio.wait_for(owner.acquire(UPLOAD_REF), 0.5)
                self.assertEqual(owner.requests_started, 4)
                self.assertIs(owner.recovery_failure, notices[0])
                if broken:
                    self.assertEqual(caught.exception.cause.exceptions, (notices[0], original_notice))
                else:
                    self.assertIs(caught.exception.cause, notices[0])

    async def test_caller_cancellation_preserves_identity_and_current_outage(self):
        waiting, cancellations, notices = asyncio.Event(), [], []
        def notice(failure, _seconds):
            notices.append(failure)
            waiting.set()
        owner, _, _ = self.owner(recovery=self.policy(on_retry=notice, initial=1))
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error())]
        async def acquire():
            try:
                return await owner.acquire(UPLOAD_REF)
            except asyncio.CancelledError as cancellation:
                cancellations.append(cancellation)
                raise
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            task = asyncio.create_task(acquire())
            await asyncio.wait_for(waiting.wait(), 0.5)
            task.cancel("caller stop")
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertIs(caught.exception, cancellations[0])
        self.assertIs(owner.recovery_failure, notices[0])
        self.assertEqual(owner.requests_started, 4)

    async def test_original_deadline_is_not_extended_by_waiting(self):
        owner, _, _ = self.owner(recovery=self.policy(initial=1), seconds=0.08)
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), api_reply(GRANT), store_reply(failure=temporary_read_error())]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadFailed, "deadline"):
                await asyncio.wait_for(owner.acquire(UPLOAD_REF), 0.5)
        self.assertEqual(owner.requests_started, 4)

    async def test_first_outage_and_first_uncertain_issuance_are_bounded_exact_references(self):
        owner, _, _ = self.owner(recovery=self.policy(attempts=3))
        original = httpcore.ReadTimeout("protected lost reply")
        class LostReply(Stream):
            async def __aiter__(self):
                with self.network.io("read"):
                    raise original
                yield b"unreachable"
        lost = Client(LostReply([]), headers=response_headers())
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), refusal(409, "upload_read_not_finalized"),
                   api_reply(MEMBERSHIP), lost, api_reply(MEMBERSHIP), refusal(409, "upload_read_not_finalized")]
        replies = iter(clients)
        def pool(**options):
            client = next(replies)
            if client is lost:
                client.stream.network = options["network_backend"]
            return client
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=pool):
            await owner.inspect()
            with self.assertRaises(UploadRecoveryExhausted) as caught:
                await owner.acquire(UPLOAD_REF)
        record = owner.recovery_failure
        self.assertIsInstance(record, BaseExceptionGroup)
        self.assertEqual(len(record.exceptions), 2)
        first, uncertain = record.exceptions
        self.assertEqual(first.problem_code, "upload_read_not_finalized")
        self.assertFalse(first.issuance_may_have_committed)
        self.assertTrue(uncertain.issuance_may_have_committed)
        self.assertFalse(caught.exception.cause.issuance_may_have_committed)
        self.assertIs(owner.recovery_failure, record)
        self.assertEqual(owner.requests_started, 7)
        self.assertEqual(owner.received_bytes, 0)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=api_reply(MEMBERSHIP)):
            await owner.inspect()
        self.assertIsNone(owner.recovery_failure)

    async def test_late_lost_grant_reply_retires_exact_api_before_fresh_signing(self):
        released, notices = asyncio.Event(), []
        original = httpcore.ReadTimeout("protected late network failure")
        class LateReply(Stream):
            async def __aiter__(self):
                with self.network.io("read"):
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await released.wait()
                        raise original
                yield b"unreachable"
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=0.03, retirement_seconds=0.02)
        owner, api, _ = self.owner(recovery=self.policy(on_retry=lambda failure, _: notices.append(failure)),
                                   api_transport=transport)
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), Client(LateReply([]), headers=response_headers()),
                   api_reply(MEMBERSHIP), api_reply(GRANT), store_reply()]
        replies = iter(clients)
        def pool(**options):
            client = next(replies)
            if client is clients[2]:
                client.stream.network = options["network_backend"]
            return client
        retire = api.retire
        async def observe_retirement(*, owner=None):
            self.assertIsNone(owner)
            self.assertFalse(transport.retirement_confirmed)
            released.set()
            await retire(owner=owner)
            self.assertTrue(transport.retirement_confirmed)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=pool), patch.object(
            api, "retire", side_effect=observe_retirement
        ):
            try:
                await owner.inspect()
                result = await owner.acquire(UPLOAD_REF)
            finally:
                released.set()
                await retire()
        self.assertEqual(result.data, BODY)
        self.assertIs(owner.recovery_failure, notices[0])
        self.assertTrue(owner.recovery_failure.issuance_may_have_committed)
        self.assertIs(owner.recovery_failure.cause.primary, original)
        self.assertIs(owner.recovery_failure.cause.network_failure, original)
        self.assertIsNone(owner.last_failure)
        first_headers = {name.lower(): value for name, value in clients[2].calls[0][0].headers}
        second_headers = {name.lower(): value for name, value in clients[4].calls[0][0].headers}
        self.assertNotEqual(first_headers[b'signature-input'], second_headers[b'signature-input'])
        self.assertEqual(owner.requests_started, 6)

    async def test_file_grant_renewal_keeps_private_file_cleanup_before_fresh_access(self):
        notices = []
        owner, _, _ = self.owner(recovery=self.policy(on_retry=lambda failure, _: notices.append(failure)))
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), refusal(409, "upload_read_not_finalized"),
                   api_reply(MEMBERSHIP), api_reply(GRANT), store_reply()]
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients
        ):
            await owner.inspect()
            receipt = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertEqual(Path(root, receipt.name).read_bytes(), BODY)
            self.assertEqual(len(list(Path(root).iterdir())), 1)
            self.assertIs(owner.recovery_failure, notices[0].cause)
            self.assertEqual(owner.recovery_failure.problem_code, "upload_read_not_finalized")
            self.assertIsNone(owner.last_failure)
            os.unlink(receipt.name, dir_fd=directory)
        self.assertEqual(owner.requests_started, 6)
        self.assertEqual(owner.received_bytes, 8)

    async def test_total_grant_timeout_keeps_earlier_outage_and_new_issuance_uncertainty(self):
        owner, api, _ = self.owner(recovery=self.policy(), seconds=0.08)
        clients = [api_reply(MEMBERSHIP), api_reply(MEMBERSHIP), refusal(409, "upload_read_not_finalized"),
                   api_reply(MEMBERSHIP), Client(Stream([], stall=asyncio.Event()), headers=response_headers())]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadFailed, "deadline"):
                await owner.acquire(UPLOAD_REF)
        self.assertIsInstance(owner.recovery_failure, BaseExceptionGroup)
        first, uncertain = owner.recovery_failure.exceptions
        self.assertEqual(first.problem_code, "upload_read_not_finalized")
        self.assertIs(uncertain, api.last_failure)
        self.assertTrue(uncertain.issuance_may_have_committed)
        self.assertEqual(owner.requests_started, 5)

    async def test_admitted_api_retry_after_is_floor_above_local_maximum(self):
        stopping, notices = asyncio.Event(), []
        def notice(failure, seconds):
            notices.append((failure, seconds))
            stopping.set()
        owner, _, _ = self.owner(recovery=self.policy(stopping=stopping, on_retry=notice))
        response = refusal(503, "service_unavailable")
        response.headers += ((b"retry-after", b"1"),)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=response):
            with self.assertRaises(UploadFailed):
                await owner.inspect()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][1], 1)
        self.assertIs(owner.recovery_failure, notices[0][0])
        self.assertEqual(notices[0][0].retry_after_seconds, 1)
        self.assertEqual(owner.requests_started, 1)

    async def test_local_jitter_is_bounded_and_does_not_consume_scientific_rng(self):
        for sample in (0.01, 0.02):
            with self.subTest(sample=sample):
                stopping, notices = asyncio.Event(), []
                def notice(_failure, seconds):
                    notices.append(seconds)
                    stopping.set()
                owner, _, _ = self.owner(recovery=self.policy(stopping=stopping, on_retry=notice, initial=0.02))
                response = refusal(503, "service_unavailable")
                scientific_state = random.getstate()
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=response), patch(
                    "provider_core.uploads.secrets.SystemRandom.uniform", return_value=sample,
                ) as jitter:
                    with self.assertRaises(UploadFailed):
                        await owner.inspect()
                jitter.assert_called_once_with(0.01, 0.02)
                self.assertEqual(notices, [sample])
                self.assertEqual(random.getstate(), scientific_state)
                self.assertEqual(owner.requests_started, 1)

    async def test_server_floor_that_cannot_fit_deadline_prevents_notice_and_next_read(self):
        notices = []
        owner, _, _ = self.owner(recovery=self.policy(on_retry=lambda *facts: notices.append(facts)), seconds=0.1)
        response = refusal(503, "service_unavailable")
        response.headers += ((b"retry-after", b"1"),)
        clients = [response, api_reply(MEMBERSHIP)]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            with self.assertRaisesRegex(UploadFailed, "deadline") as caught:
                await asyncio.wait_for(owner.inspect(), 0.5)
        self.assertEqual(notices, [])
        self.assertEqual(owner.requests_started, 1)
        self.assertIs(type(caught.exception), UploadRecoveryExhausted)
        outage = caught.exception.cause
        self.assertIs(outage, owner.recovery_failure)
        self.assertEqual(outage.retry_after_seconds, 1)
        self.assertIn("1", caught.exception.reason)
        self.assertEqual(clients[1].calls, [])

    async def test_retry_notice_cannot_extend_original_wait_budget(self):
        notices = []
        def notice(failure, seconds):
            notices.append((failure, seconds))
            time.sleep(0.15)
        owner, _, _ = self.owner(recovery=self.policy(on_retry=notice, initial=0.2), seconds=0.3)
        response = refusal(503, "service_unavailable")
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=response), patch(
            "provider_core.uploads.secrets.SystemRandom.uniform", return_value=0.2
        ):
            with self.assertRaisesRegex(UploadFailed, "deadline") as caught:
                await asyncio.wait_for(owner.inspect(), 0.5)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][1], 0.2)
        self.assertIs(type(caught.exception), UploadRecoveryExhausted)
        self.assertIs(caught.exception.cause, notices[0][0])
        self.assertIn("remaining", caught.exception.reason)
        self.assertEqual(owner.requests_started, 1)
