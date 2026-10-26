"""Acquisition accounts failed work and refuses changed or foreign sources."""

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.upload_response import ReadGrant, UploadMember
from provider_core.uploads import UploadFailed, UploadInputRejected, VerifiedUpload
from tests.fakes.acquisition import sources, store_headers
from tests.fakes.uploads import UPLOAD_REF


class UploadAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_refresh_that_spends_the_last_request_stops_without_a_correction(self):
        owner, api, store = sources(requests=2)
        await owner.inspect()
        api.member = replace(api.member, description="Changed description")
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertNotIsInstance(caught.exception, UploadInputRejected)
        self.assertIs(owner.last_failure, caught.exception)
        self.assertFalse(caught.exception.grant_issued)
        self.assertEqual(owner.requests_started, 2)
        self.assertEqual(owner.received_bytes, 0)
        self.assertEqual(api.calls, ["membership", "membership"])
        self.assertEqual(store.calls, [])

    async def test_fixed_file_ceiling_is_correctable_at_one_byte_over_without_grant_or_budget_reset(self):
        owner, api, store = sources(source_bytes=7)
        await owner.inspect()
        with self.assertRaises(UploadInputRejected) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertIs(owner.last_failure, caught.exception)
        self.assertFalse(caught.exception.grant_issued)
        self.assertIn("8 bytes", str(caught.exception))
        self.assertIn("7-byte", str(caught.exception))
        self.assertEqual(owner.requests_started, 1)
        self.assertEqual(owner.received_bytes, 0)
        self.assertEqual(api.calls, ["membership"])
        self.assertEqual(store.calls, [])
        with self.assertRaisesRegex(UploadInputRejected, "inspect"):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 1)

    async def test_spent_cumulative_bytes_dominate_a_simultaneous_fixed_file_ceiling(self):
        owner, api, store = sources(source_bytes=8, total_bytes=8)
        store.headers = store_headers(store.body)
        await owner.inspect()
        await owner.acquire(UPLOAD_REF)
        await owner.inspect()
        # Both the fixed ceiling and spent cumulative budget reject this member;
        # spent work must remain an operational hold rather than invite repair.
        api.member = replace(api.member, byte_length=9)
        await owner.inspect()
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertNotIsInstance(caught.exception, UploadInputRejected)
        self.assertFalse(caught.exception.grant_issued)
        self.assertEqual(owner.received_bytes, 8)
        self.assertEqual(owner.requests_started, 6)
        self.assertEqual(api.calls.count("grant"), 1)
        self.assertEqual(len(store.calls), 1)

    async def test_spent_requests_or_deadline_dominate_an_oversized_file_refusal(self):
        owner, api, store = sources(source_bytes=7, requests=1)
        await owner.inspect()
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertNotIsInstance(caught.exception, UploadInputRejected)
        self.assertEqual(owner.requests_started, 1)
        self.assertEqual(api.calls, ["membership"])
        self.assertEqual(store.calls, [])
        with patch("provider_core.uploads.time") as clock:
            clock.monotonic.return_value = 100
            owner, api, store = sources(source_bytes=7)
            await owner.inspect()
            clock.monotonic.return_value = 161
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire(UPLOAD_REF)
            self.assertNotIsInstance(caught.exception, UploadInputRejected)
            self.assertEqual(owner.requests_started, 1)
            self.assertEqual(api.calls, ["membership"])
            self.assertEqual(store.calls, [])

    async def test_changed_membership_requires_reinspection_before_any_grant(self):
        owner, api, store = sources()
        await owner.inspect()
        api.member = UploadMember(UPLOAD_REF, "changed description", 8, api.member.content_hash)
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(api.calls, ["membership", "membership"])
        self.assertEqual(store.calls, [])
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(api.calls, ["membership", "membership"])

    async def test_source_size_is_admitted_before_grant(self):
        owner, api, store = sources(source_bytes=4)
        await owner.inspect()
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertNotIn("grant", api.calls)
        self.assertEqual(store.calls, [])

    async def test_foreign_url_never_receives_bearer(self):
        owner, api, store = sources()
        original = api.grant
        api.grant = ReadGrant(
            original.job_ref,
            original.upload_ref,
            original.byte_length,
            original.content_hash,
            "https://foreign.example/bytes",
            original.capability,
            original.expires_at,
        )
        await owner.inspect()
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertNotIn("private-bearer", str(caught.exception))
        self.assertTrue(caught.exception.grant_issued)
        self.assertEqual(store.calls, [])

    async def test_failed_download_bytes_are_charged_and_not_refunded(self):
        owner, api, store = sources(total_bytes=8)
        await owner.inspect()
        # The malformed store headers make these bytes unusable, but still spent.
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.received_bytes, 8)
        await owner.inspect()
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(api.calls.count("grant"), 1)
        self.assertEqual(len(store.calls), 1)

    async def test_expired_deadline_after_hashing_never_returns_late_bytes(self):
        with patch("provider_core.uploads.time") as clock:
            clock.monotonic.return_value = 100
            owner, _, store = sources()
            store.headers = store_headers(store.body)
            await owner.inspect()

            def delayed_verification(*args):
                verified = VerifiedUpload(*args)
                clock.monotonic.return_value = 161
                return verified

            with patch("provider_core.uploads.VerifiedUpload", side_effect=delayed_verification), self.assertRaises(UploadFailed):
                await owner.acquire(UPLOAD_REF)

    async def test_request_budget_includes_inspection_refresh_and_grant(self):
        owner, api, store = sources(requests=3)
        await owner.inspect()
        with self.assertRaises(UploadFailed):
            await owner.acquire(UPLOAD_REF)
        self.assertEqual(owner.requests_started, 3)
        self.assertEqual(api.calls, ["membership", "membership", "grant"])
        self.assertEqual(store.calls, [])
        with self.assertRaises(UploadFailed):
            await owner.inspect()
        self.assertEqual(owner.requests_started, 3)

    async def test_unconfirmed_store_retirement_blocks_even_membership_refresh(self):
        owner, api, store = sources()
        store.retirement_confirmed = False
        with self.assertRaises(UploadFailed):
            await owner.inspect()
        self.assertEqual(api.calls, [])

    async def test_failed_grant_requires_explicit_reinspection_for_changed_or_unchanged_list(self):
        for changed in (False, True):
            owner, api, store = sources()
            original = api.grant
            failure = ApiFailure("issue Upload read grant", "unavailable")
            failure.issuance_may_have_committed = True
            api.grant = failure
            await owner.inspect()
            with self.assertRaises(ApiFailure) as caught:
                await owner.acquire(UPLOAD_REF)
            self.assertIs(caught.exception, failure)
            with self.assertRaises(UploadFailed):
                await owner.acquire(UPLOAD_REF)
            self.assertEqual(api.calls.count("grant"), 1)
            api.grant = original
            if changed:
                api.member = None
            observed = await owner.inspect()
            # silent-defaults: allow-contract -- Changed membership remains empty until an explicit refreshed inspection.
            self.assertEqual(len(observed.members), 0 if changed else 1)
            self.assertEqual(store.calls, [])

    async def test_expired_or_changed_grant_never_reaches_store(self):
        for changes in (
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            {"content_hash": "sha256:" + "f" * 64},
            {"byte_length": 9},
        ):
            owner, api, store = sources()
            api.grant = replace(api.grant, **changes)
            await owner.inspect()
            with self.assertRaises(UploadFailed):
                await owner.acquire(UPLOAD_REF)
            self.assertEqual(store.calls, [])

    async def test_same_length_corrupted_bytes_are_charged_but_never_returned(self):
        owner, _, store = sources()
        store.headers = store_headers(store.body)
        store.body = b"tampered"
        await owner.inspect()
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertIn("SHA-256", str(caught.exception))
        self.assertTrue(caught.exception.grant_issued)
        self.assertEqual(owner.received_bytes, 8)

    async def test_deadline_during_grant_preserves_possible_issuance(self):
        owner, api, store = sources(seconds=0.05)
        failure = ApiFailure("issue Upload read grant", "reply interrupted")
        failure.issuance_may_have_committed = True

        async def stalled_grant(_job_ref, _upload_ref):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                api.last_failure = failure
                raise

        api.read_grant = stalled_grant
        await owner.inspect()
        with self.assertRaises(UploadFailed) as caught:
            await owner.acquire(UPLOAD_REF)
        self.assertIs(caught.exception.cause, failure)
        self.assertFalse(caught.exception.grant_issued)
        self.assertTrue(caught.exception.cause.issuance_may_have_committed)
        self.assertEqual(store.calls, [])

    async def test_overlapping_inspection_is_rejected_without_poisoning_active_selection(self):
        owner, api, _ = sources()
        entered, release = asyncio.Event(), asyncio.Event()
        original = api.uploads

        async def delayed_inspection(job_ref):
            entered.set()
            await release.wait()
            return await original(job_ref)

        api.uploads = delayed_inspection
        active = asyncio.create_task(owner.inspect())
        await entered.wait()
        try:
            with self.assertRaises(UploadFailed):
                await owner.inspect()
        finally:
            release.set()
            observed = await active
        self.assertEqual(len(observed.members), 1)
        self.assertEqual(api.calls, ["membership"])
