"""Release drift and future wire responses fail without accepting partial facts."""

import base64
import json
import re
import shutil
import tempfile
import unittest
import os
import pathlib

from provider_core.api import ApiFailure
from provider_core.job_input import JobInputRejected, verify_job_input
from tests.contract.api_release import RELEASE_ROOT, PinnedApiRelease
from tests.contract.provider_calls import provider_calls
from tests.fakes.api import Transport, reply
from tests.fakes.job_input import facts
from tests.fakes.job_input import response as input_response
from tests.fakes.api import api


class ReleasedApiRefusalTests(PinnedApiRelease, unittest.IsolatedAsyncioTestCase):
    def test_noncanonical_base64_pad_bits_are_refused_even_when_decoded_bytes_match(self):
        source = b"x"
        document = json.loads(input_response(source))

        self.assertEqual(document["canonical_input_base64"], "eA==")
        document["canonical_input_base64"] = "eB=="

        self.assertEqual(base64.b64decode(document["canonical_input_base64"], validate=True), source)
        pattern = self.schemas["nmr.provider.job_input.read.response.v1"]["properties"]["canonical_input_base64"]["pattern"]

        self.assertIsNone(re.fullmatch(pattern, document["canonical_input_base64"]))

        with self.assertRaises(JobInputRejected):
            verify_job_input(json.dumps(document).encode(), feed=facts(source))

    def test_changed_or_incomplete_release_is_not_a_compatible_fixture(self):
        for mutation in ("artifact", "manifest", "missing", "extra", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory) / "release"

                shutil.copytree(RELEASE_ROOT, root)
                path = root / "schemas/hello_response.v1.schema.json"

                if mutation == "artifact":
                    path.write_bytes(path.read_bytes().replace(b"accepted_at", b"accepted_on"))
                elif mutation == "manifest":
                    path = root / "manifest.json"

                    path.write_bytes(path.read_bytes().replace(b"12a53e72", b"22a53e72"))
                elif mutation == "missing":
                    path.unlink()
                elif mutation == "extra":
                    (root / "unlisted.json").write_bytes(b"{}")
                else:
                    outside = pathlib.Path(directory) / "unchanged-schema.json"

                    path.rename(outside)
                    path.symlink_to(outside)

                with self.assertRaises(AssertionError):
                    self.read_release(root)

    async def _changed_responses(self):
        for (operation, call) in provider_calls().items():
            for mutation in ("schema", "extra"):
                with self.subTest(operation=operation, mutation=mutation):
                    document = json.loads(call.response)

                    if mutation == "schema":
                        document["schema_id"] += ".future"
                    else:
                        document["unreviewed_protocol_field"] = "PRIVATE_PROTOCOL_CANARY"

                    transport = Transport(reply(json.dumps(document).encode()))
                    client = api(transport, "https://api.example.test")

                    with self.assertRaises(ApiFailure) as failed:
                        await getattr(client, call.method_name)(*call.arguments, **call.options)

                    yield transport, client, failed.exception

    async def test_each_operation_refuses_changed_schema_and_unknown_top_level_response_fields(self):
        async for (transport, client, failure) in self._changed_responses():
            self.assertEqual(len(transport.calls), 1)
            self.assertIs(client.last_failure, failure)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_response_refusals_omit_unknown_fields(self):
        async for (_transport, _client, failure) in self._changed_responses():
            self.assertNotIn("PRIVATE_PROTOCOL_CANARY", str(failure))
