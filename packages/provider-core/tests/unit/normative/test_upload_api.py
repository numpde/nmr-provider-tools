"""Membership and download grants retain distinct Job/resource/content facts."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.uploads import CONTENT_HASH, UPLOAD_REF, encoded, grant, member, membership


class UploadApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_membership_and_grant_use_exact_bodyless_signed_routes(self):
        transport = Transport(reply(encoded(membership(uploads=[member(content_hash=None)]))), reply(encoded(grant())))
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        observed = await api.uploads("job:example")
        self.assertEqual(observed.job_ref, "job:example")
        self.assertEqual(observed.members[0].description, "Two conformers\n雪")
        self.assertIsNone(observed.members[0].content_hash)
        access = await api.read_grant("job:example", UPLOAD_REF)
        self.assertEqual((access.job_ref, access.upload_ref, access.content_hash), ("job:example", UPLOAD_REF, CONTENT_HASH))
        self.assertNotIn("private-bearer-canary", repr(access))
        for (request, ceiling), method, target, limit in zip(
            transport.calls,
            ("GET", "POST"),
            (
                b"/provider/v1/jobs/job:example/uploads",
                b"/provider/v1/jobs/job:example/uploads/" + UPLOAD_REF.encode() + b"/read-capability",
            ),
            (2097152, 65536),
            strict=True,
        ):
            self.assertEqual((request.method, request.url.raw_path, ceiling), (method, target, limit))
            self.assertNotIn("Content-Type", request.headers)
            self.assertNotIn("Content-Digest", request.headers)
            self.assertIn("Signature", request.headers)

    async def test_empty_and_maximum_membership_preserve_all_members_and_descriptions(self):
        for members in (
            [],
            [member(upload_ref="upload:sha256:" + f"{index:064x}", description="\x01" * 4096) for index in range(64)],
        ):
            transport = Transport(reply(encoded(membership(uploads=members))))
            api = ProviderAPI(
                SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            observed = await api.uploads("job:example")
            self.assertEqual(len(observed.members), len(members))
            self.assertEqual([item.description for item in observed.members], [item["description"] for item in members])
