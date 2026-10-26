"""Only a refreshed source and its verified complete bytes cross to consumers."""

import unittest
from dataclasses import replace

from tests.fakes.acquisition import sources, store_headers
from tests.fakes.uploads import UPLOAD_REF


class UploadAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_at_exact_fixed_ceiling_acquires_once_and_charges_the_full_bytes(self):
        owner, api, store = sources(source_bytes=8)
        store.headers = store_headers(store.body)
        await owner.inspect()
        verified = await owner.acquire(UPLOAD_REF)
        self.assertEqual(verified.data, b"geometry")
        self.assertEqual(owner.received_bytes, 8)
        self.assertEqual(owner.requests_started, 4)
        self.assertEqual(api.calls, ["membership", "membership", "grant"])
        self.assertEqual(len(store.calls), 1)

    async def test_pending_or_finalized_source_returns_exact_immutable_bytes_without_signing_store_request(self):
        for pending in (False, True):
            owner, api, store = sources()
            if pending:
                api.member = replace(api.member, content_hash=None)
            store.headers = store_headers(store.body)
            await owner.inspect()
            verified = await owner.acquire(UPLOAD_REF)
            self.assertEqual(verified.data, b"geometry")
            self.assertIsInstance(verified.data, bytes)
            self.assertEqual(verified.content_hash, api.grant.content_hash)
            self.assertEqual(owner.received_bytes, 8)
            self.assertEqual(store.maximum_bytes, 8)
            self.assertEqual(owner.requests_started, 4)
            self.assertEqual(api.calls, ["membership", "membership", "grant"])
            request = store.calls[0]
            self.assertEqual(request.headers["Authorization"], "Bearer private-bearer")
            for forbidden in ("Signature", "Signature-Input", "Content-Type", "Content-Length", "Range"):
                self.assertNotIn(forbidden, request.headers)
