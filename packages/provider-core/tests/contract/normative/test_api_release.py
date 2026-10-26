"""Installed signing and all provider routes match the selected API release.

These are concrete consumed-rule checks, not a general JSON Schema validator,
server compatibility attestation or complete implementation of every OpenAPI rule.
"""

import base64
import json
import unittest
from urllib.parse import parse_qsl

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key
from http_sf import parse

from provider_core.api import require_api_topology
from provider_core.hello import AnalysisOffering, hello_body
from provider_core.job_input import JobInputRejected, verify_job_input
from provider_core.signing import SigningIdentity, signed_request
from tests.contract.api_release import PinnedApiRelease
from tests.contract.provider_calls import provider_calls
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.job_input import facts
from tests.fakes.job_input import response as input_response
from tests.fakes.api import api
from tests.fakes.uploads import UPLOAD_REF


class ReleasedApiContractTests(PinnedApiRelease, unittest.IsolatedAsyncioTestCase):
    def test_job_input_preserves_exact_bytes_at_the_released_length_bounds(self):
        schema = self.schemas["nmr.provider.job_input.read.response.v1"]
        limits = schema["properties"]["input_byte_length"]
        for length in (limits["minimum"], limits["maximum"]):
            with self.subTest(length=length):
                source = b"x" * length
                wire = input_response(source)
                self.assertEqual(set(json.loads(wire)), set(schema["required"]))
                prepared = verify_job_input(wire, feed=facts(source))
                self.assertEqual(prepared.specification_bytes, source)
                self.assertLessEqual(
                    len(json.loads(wire)["canonical_input_base64"]),
                    schema["properties"]["canonical_input_base64"]["maxLength"],
                )
        with self.assertRaises(JobInputRejected):
            facts(b"x" * (limits["maximum"] + 1))

    def test_hello_accepts_released_unicode_scalar_limits_and_refuses_overflow(self):
        properties = self.schemas["nmr.provider.hello_request.v1"]["properties"]
        offering = properties["analysis_offerings"]
        name = "𐍈" * properties["display_name"]["maxLength"]
        description = "𐍈" * properties["description"]["maxLength"]
        # repo-check: allow-tuple-generator -- The Hello codec requires a tuple so this fixture exercises the actual wire contract.
        offerings = tuple(AnalysisOffering(f"model_{index}", description) for index in range(offering["maxItems"]))
        body = hello_body(display_name=name, description=description, analysis_offerings=offerings)
        self.assertEqual(json.loads(body)["display_name"], name)
        self.assertEqual(json.loads(body)["analysis_offerings"][-1]["description"], description)
        with self.assertRaises(ValueError):
            hello_body(display_name=name + "x", description=description, analysis_offerings=offerings)
        with self.assertRaises(ValueError):
            hello_body(display_name=name, description=description + "x", analysis_offerings=offerings)
        with self.assertRaises(ValueError):
            hello_body(
                display_name=name,
                description=description,
                analysis_offerings=(*offerings, AnalysisOffering("another_model", description)),
            )

    def test_signer_uses_the_released_bases_and_published_signatures_verify(self):
        vectors = self.openapi["x-nmr-signature-conformance-vectors"]
        self.assertEqual({item["name"] for item in vectors}, {"provider-jobs-list-get", "execution-attempt-start-post"})
        self.assertEqual(len(vectors), 2)
        key = Ed25519PrivateKey.generate()
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                # silent-defaults: allow-contract -- Released null means no body; empty bytes would still require a content digest.
                body = vector["body_utf8"].encode() if vector["body_utf8"] is not None else None
                request = signed_request(
                    SigningIdentity("https://" + vector["authority"], vector["keyid"], key),
                    method=vector["method"],
                    path=vector["raw_path"],
                    query=vector["raw_query"].encode(),
                    body=body,
                    created=vector["created"],
                    nonce=base64.urlsafe_b64decode(vector["nonce"] + "=="),
                )
                if body is None:
                    self.assertEqual(request.content, b"")
                else:
                    self.assertEqual(request.content, body)
                self.assertEqual(request.headers["Signature-Input"], vector["signature_input"])
                self.assertEqual(request.headers.get("Content-Digest"), vector["content_digest"])
                self.assertEqual(request.headers.get("Content-Type"), vector["content_type"])
                # silent-defaults: allow-contract -- Empty raw query adds no URL delimiter; the released signature base still covers '?'.
                target = vector["raw_path"] + ("?" + vector["raw_query"] if vector["raw_query"] else "")
                self.assertEqual(request.url.raw_path, target.encode())
                signature = parse(request.headers["Signature"].encode(), tltype="dictionary")["sig1"][0]
                # Verify against the RELEASED bytes, not a base reconstructed by
                # core or this test. Vector signing keys are neither known nor needed.
                key.public_key().verify(signature, vector["signature_base"].encode())
                published = load_der_public_key(base64.b64decode(vector["public_key_spki_der_b64"]))
                self.assertIsInstance(published, Ed25519PublicKey)
                published_signature = parse(vector["signature"].encode(), tltype="dictionary")["sig1"][0]
                published.verify(published_signature, vector["signature_base"].encode())

    async def test_all_eleven_dispatches_use_released_routes_ceilings_and_closed_top_level_fields(self):
        routes = {
            operation["operationId"]: (method.upper(), path, operation)
            for path, item in self.openapi["paths"].items()
            for method, operation in item.items()
        }
        calls = provider_calls()
        self.assertEqual(set(calls), set(routes))
        self.assertEqual(len(calls), 11)
        for operation_id, call in calls.items():
            with self.subTest(operation=operation_id):
                transport = Transport(reply(call.response))
                client = api(transport, "https://api.example.test")
                await getattr(client, call.method_name)(*call.arguments, **call.options)
                ((request, maximum_bytes),) = transport.calls
                self.assertEqual(request.content, call.body)
                method, template, operation = routes[operation_id]
                path = template.format(job_ref="job:example", execution_attempt_ref=ATTEMPT_REF, upload_ref=UPLOAD_REF)
                self.assertEqual(request.method, method)
                raw_path, _, query = request.url.raw_path.partition(b"?")
                self.assertEqual(raw_path, path.encode())
                self.assertEqual(maximum_bytes, operation["x-nmr-max-response-bytes"])
                self.assertLessEqual(len(query), operation["x-nmr-max-query-bytes"])
                parameters = {item["name"]: item for item in operation["parameters"] if item["in"] == "query"}
                names = {name for name, _ in parse_qsl(query.decode(), keep_blank_values=True)}
                self.assertLessEqual(names, set(parameters))
                self.assertLessEqual({name for name, item in parameters.items() if item["required"]}, names)
                response_schema = self.schemas[operation["x-nmr-response-schema-id"]]
                self.assertEqual(json.loads(call.response)["schema_id"], response_schema["properties"]["schema_id"]["const"])
                self.assertEqual(set(json.loads(call.response)), set(response_schema["required"]))
                self.assertFalse(response_schema["additionalProperties"])
                self.assertLessEqual(len(request.content), operation["x-nmr-max-request-body-bytes"])
                if "x-nmr-request-schema-id" in operation:
                    schema = self.schemas[operation["x-nmr-request-schema-id"]]
                    document = json.loads(request.content)
                    self.assertEqual(document["schema_id"], schema["properties"]["schema_id"]["const"])
                    self.assertEqual(set(document), set(schema["required"]))
                    self.assertFalse(schema["additionalProperties"])
                else:
                    self.assertEqual(request.content, b"")

    def test_topology_vocabulary_is_the_released_routed_environment_vocabulary(self):
        topologies = self.openapi["components"]["headers"]["NmrApiTopology"]["schema"]["enum"]
        self.assertEqual(set(topologies), {"dev-local", "dev", "web"})
        for topology in topologies:
            self.assertEqual(require_api_topology(topology), topology)
        with self.assertRaises(ValueError):
            require_api_topology("unreviewed-topology")
