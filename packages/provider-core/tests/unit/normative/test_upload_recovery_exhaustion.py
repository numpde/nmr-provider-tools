"""Only settled, currently eligible outages produce recovery exhaustion."""

import asyncio
import errno
import unittest
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.uploads import UploadFailed, UploadRecoveryExhausted
from tests.fakes.acquisition import private_source_directory
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF
from tests.unit.normative import test_upload_recovery as fixture
from tests.unit.normative.test_upload_transfer_retry import temporary_read_error


class UploadRecoveryExhaustionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture.UploadRecoveryTests()

    async def test_final_eligible_api_failure_is_classified_after_exact_retirement(self):
        owner, api, store = self.fixture.owner(recovery=self.fixture.policy(attempts=1))
        client = fixture.refusal(503, "service_unavailable")
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client), patch.object(
            api, "retire", wraps=api.retire
        ) as api_retire, patch.object(store, "retire", wraps=store.retire) as store_retire:
            with self.assertRaises(UploadRecoveryExhausted) as caught:
                await owner.inspect()
        self.assertIs(caught.exception.cause, owner.recovery_failure)
        self.assertIs(type(caught.exception.cause), ApiFailure)
        self.assertFalse(caught.exception.grant_issued)
        self.assertTrue(api.retirement_confirmed)
        self.assertTrue(store.retirement_confirmed)
        api_retire.assert_awaited_once_with()
        store_retire.assert_awaited_once_with(owner=None)
        self.assertEqual(owner.requests_started, 1)

    async def test_exhausted_file_failure_preserves_all_upload_owner_fields(self):
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(attempts=1))
        clients = [fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.MEMBERSHIP),
                   fixture.api_reply(fixture.GRANT), fixture.store_reply(failure=temporary_read_error())]
        with private_source_directory() as (_, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients
        ):
            await owner.inspect()
            with self.assertRaises(UploadRecoveryExhausted) as caught:
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
        failure, original = caught.exception, owner.recovery_failure
        self.assertIs(failure.cause, original)
        self.assertIs(failure.response, original.response)
        self.assertIs(failure.cleanup_failures, original.cleanup_failures)
        self.assertIs(failure.interruption, original.interruption)
        self.assertEqual(failure.grant_issued, original.grant_issued)
        self.assertTrue(failure.grant_issued)
        self.assertEqual(owner.received_bytes, 1)
        self.assertEqual(owner.requests_started, 4)

    async def test_request_preflight_exhausts_before_a_partial_third_attempt(self):
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy())
        first, current = temporary_read_error(), temporary_read_error()
        clients = [fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.MEMBERSHIP),
                   fixture.api_reply(fixture.GRANT), fixture.store_reply(failure=first),
                   fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.GRANT), fixture.store_reply(failure=current)]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadRecoveryExhausted, "3 requests.*1 remain") as caught:
                await owner.acquire(UPLOAD_REF)
        self.assertIs(caught.exception.cause.cause.primary, current)
        self.assertIs(owner.recovery_failure.cause.primary, first)
        self.assertEqual(owner.requests_started, 7)
        self.assertEqual(owner.received_bytes, 2)

    async def test_original_byte_and_wait_budgets_end_only_eligible_recovery(self):
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(), total=8)
        clients = [fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.MEMBERSHIP),
                   fixture.api_reply(fixture.GRANT), fixture.store_reply(failure=temporary_read_error())]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadRecoveryExhausted, "remaining byte budget"):
                await owner.acquire(UPLOAD_REF)
        self.assertEqual((owner.requests_started, owner.received_bytes), (4, 1))
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(), seconds=0.1)
        client = fixture.refusal(503, "service_unavailable")
        client.headers += ((b"retry-after", b"1"),)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            with self.assertRaisesRegex(UploadRecoveryExhausted, "deadline") as caught:
                await owner.inspect()
        self.assertIs(caught.exception.cause, owner.recovery_failure)
        self.assertEqual(owner.requests_started, 1)

    async def test_one_shot_and_final_unknown_failure_keep_their_original_types(self):
        owner, _, _ = self.fixture.owner()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool",
                   return_value=fixture.refusal(503, "service_unavailable")):
            with self.assertRaises(ApiFailure) as caught:
                await owner.inspect()
        self.assertIs(type(caught.exception), ApiFailure)
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(attempts=1))
        client = Client(Stream([], failure=RuntimeError("unknown network implementation fault")))
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            with self.assertRaises(ApiFailure) as caught:
                await owner.inspect()
        self.assertIs(type(caught.exception), ApiFailure)

    async def test_final_disk_failure_is_not_an_eligible_outage(self):
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(attempts=1))
        clients = [fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.MEMBERSHIP),
                   fixture.api_reply(fixture.GRANT), fixture.store_reply()]
        with private_source_directory() as (_, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients
        ), patch("provider_core.transport.os.write", side_effect=OSError(errno.EACCES, "private disk refusal")):
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
        self.assertIs(type(caught.exception), UploadFailed)
        self.assertEqual(owner.received_bytes, 8)

    async def test_stop_notice_failure_and_unconfirmed_cleanup_do_not_become_exhaustion(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                stopping = asyncio.Event()
                def notice(_failure, _seconds):
                    if stopped:
                        stopping.set()
                    else:
                        raise RuntimeError("private notice failure")
                policy = self.fixture.policy(stopping=stopping, on_retry=notice)
                owner, _, _ = self.fixture.owner(recovery=policy)
                with patch("provider_core.transport.httpcore.AsyncConnectionPool",
                           return_value=fixture.refusal(503, "service_unavailable")):
                    with self.assertRaises(UploadFailed) as caught:
                        await owner.inspect()
                self.assertIs(type(caught.exception), UploadFailed)
                self.assertEqual(owner.requests_started, 1)
        owner, api, _ = self.fixture.owner(recovery=self.fixture.policy(attempts=1))
        client = fixture.refusal(503, "service_unavailable")
        client.close_failure = RuntimeError("original pool closure unconfirmed")
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            with self.assertRaises(UploadFailed) as caught:
                await owner.inspect()
        self.assertIs(type(caught.exception), UploadFailed)
        self.assertFalse(api.retirement_confirmed)

    async def test_later_inflight_timeout_does_not_inherit_prior_eligibility(self):
        owner, _, _ = self.fixture.owner(recovery=self.fixture.policy(), seconds=0.1)
        rejected = Client(Stream([]), status=503, headers=[])
        hanging = Client(Stream([], stall=asyncio.Event()), headers=fixture.response_headers())
        clients = [fixture.api_reply(fixture.MEMBERSHIP), fixture.api_reply(fixture.MEMBERSHIP),
                   fixture.api_reply(fixture.GRANT), rejected,
                   fixture.api_reply(fixture.MEMBERSHIP), hanging]
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients):
            await owner.inspect()
            with self.assertRaisesRegex(UploadFailed, "deadline") as caught:
                await owner.acquire(UPLOAD_REF)
        self.assertIs(type(caught.exception), UploadFailed)
        self.assertEqual(owner.requests_started, 6)
