"""Finite TLS API/store peers prove credential separation and consumed-work limits."""

import asyncio
import hashlib
import stat
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadFailed, UploadLimits
from tests.fakes.acquisition import private_source_directory, sources, store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership


class UploadAcquisitionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_api_to_bearer_store_returns_exact_verified_bytes(self):
        await self._signed_api_to_bearer_store(file_output=False)

    async def test_signed_api_to_bearer_store_hands_off_exact_private_closed_file(self):
        await self._signed_api_to_bearer_store(file_output=True)

    async def _signed_api_to_bearer_store(self, *, file_output):
        body = b"geometry"
        fingerprint = "sha256:" + hashlib.sha256(body).hexdigest()
        # repo-check: allow-tuple-generator -- This fixed header specimen deliberately omits Content-Length for framing qualification.
        headers = tuple((key, value) for key, value in store_headers(body) if key != b"content-length")
        async with TlsPeer("success", response_body=body, response_headers=headers) as store_peer:
            upload = member(byte_length=len(body), content_hash=fingerprint)
            document = encoded(membership(uploads=[upload]))
            access = encoded(
                grant(
                    byte_length=len(body),
                    content_hash=fingerprint,
                    download_url=store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                )
            )
            async with api_tls_peer("success", response_bodies=[document, document, access]) as api_peer:
                api_transport = HttpTransport(trust=api_peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                store_transport = HttpTransport(trust=store_peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                api = ProviderAPI(
                    SigningIdentity(api_peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                    api_transport,
                    expected_topology="dev-local",
                )
                owner = JobUploads(
                    "job:example",
                    api=api,
                    store=store_transport,
                    store_origin=store_peer.origin,
                    limits=UploadLimits(100, 100, 4, 5),
                )
                await owner.inspect()
                with patch("provider_core.uploads.datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 10, 18, tzinfo=UTC)
                    if file_output:
                        with private_source_directory() as (root, directory):
                            verified = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.3)
                            self.assertEqual(Path(root, verified.name).read_bytes(), body)
                            self.assertEqual(stat.S_IMODE(Path(root, verified.name).stat().st_mode), 0o600)
                    else:
                        verified = await owner.acquire(UPLOAD_REF)
                        self.assertEqual(verified.data, body)
                self.assertEqual((verified.job_ref, verified.upload_ref, verified.byte_length), ("job:example", UPLOAD_REF, 8))
                self.assertEqual(verified.content_hash, fingerprint)
                self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
                self.assertEqual(len(api_peer.requests), 3)
                self.assertEqual(len(store_peer.requests), 1)
                self.assertTrue(all(b"Signature:" in request for request in api_peer.requests))
                self.assertTrue(all(b"private-bearer-canary" not in request for request in api_peer.requests))
                self.assertIn(b"Authorization: Bearer private-bearer-canary", store_peer.requests[0])
                self.assertNotIn(b"Signature", store_peer.requests[0])
                self.assertNotIn(b"credential:test", store_peer.requests[0])
                await asyncio.wait_for(store_peer.closed.wait(), 1)
                await api_transport.retire()
                await store_transport.retire()

    async def test_deadline_and_cancellation_charge_partial_bytes_and_close_store(self):
        body = b"x" * 100
        fingerprint = "sha256:" + hashlib.sha256(body).hexdigest()
        headers = [(key, value) for key, value in store_headers(body) if key != b"content-length"]
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                async with TlsPeer("body-stall", response_headers=headers) as peer:
                    _, api, _ = sources()
                    api.member = replace(api.member, byte_length=len(body), content_hash=fingerprint)
                    api.grant = replace(
                        api.grant, byte_length=len(body), content_hash=fingerprint,
                        download_url=peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                    )
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=0.3)
                    # silent-defaults: allow-contract -- The long budget lets cancellation win; the short budget exercises the separate deadline outcome.
                    owner = JobUploads(
                        "job:example",
                        api=api,
                        store=transport,
                        store_origin=peer.origin,
                        limits=UploadLimits(100, 100, 10, 1 if cancel else 0.15),
                    )
                    await owner.inspect()
                    acquisition = asyncio.create_task(owner.acquire(UPLOAD_REF))
                    await asyncio.wait_for(peer.received.wait(), 1)
                    if cancel:
                        # Observe actual consumption, rather than guessing from a delay.
                        async with asyncio.timeout(1):
                            while owner.received_bytes == 0:
                                await asyncio.sleep(0.001)
                        acquisition.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else UploadFailed):
                        await acquisition
                    self.assertEqual(owner.received_bytes, 1)
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    self.assertTrue(transport.retirement_confirmed)
                    self.assertIsNotNone(owner.last_failure)
                    self.assertTrue(owner.last_failure.grant_issued)
                    self.assertEqual(len(peer.requests), 1)
                    await transport.retire()

    async def test_oversized_store_head_is_rejected_before_body_budget_is_spent(self):
        headers = [(key, value) for key, value in store_headers(b"geometry") if key != b"content-length"]
        async with TlsPeer("success", response_body=b"oversized", response_headers=headers) as peer:
            _, api, _ = sources()
            api.grant = replace(api.grant, download_url=peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes")
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            owner = JobUploads(
                "job:example", api=api, store=transport, store_origin=peer.origin, limits=UploadLimits(8, 8, 10, 5)
            )
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire(UPLOAD_REF)
            self.assertIn("content-length", str(caught.exception))
            self.assertEqual(owner.received_bytes, 0)
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(transport.retirement_confirmed)
            self.assertEqual(len(peer.requests), 1)
