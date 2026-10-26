"""Lost TLS replies recover only through explicit exact terminal publication."""

import asyncio
import unittest
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_request import complete_body, fail_body
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class AttemptOutcomeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_acknowledgement_followed_by_explicit_exact_body_resend(self):
        for completing, result_bytes, message in (
            (True, b"result bytes", ""),
            (False, b"", " Explain\n雪\x01 "),
            (True, b"x" * 786432, ""),
            (False, b"", "\U0001f600" * 1024),
        ):
            response = (
                complete_response(
                    replayed=True,
                    result_fingerprint="sha256:" + sha256(result_bytes).hexdigest(),
                    result_byte_length=len(result_bytes),
                )
                if completing
                else fail_response(replayed=True, failure_message=message)
            )
            body = (
                complete_body(ATTEMPT_REF, " result\n雪 ", result_bytes)
                if completing
                else fail_body(ATTEMPT_REF, "input_rejected", message)
            )
            async with api_tls_peer("success", response_bodies=[None, response]) as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
                key = Ed25519PrivateKey.generate()
                api = ProviderAPI(SigningIdentity(peer.origin, "credential:test", key), transport, expected_topology="dev-local")
                send = api.complete_attempt if completing else api.fail_attempt
                with self.assertRaises(ApiFailure) as lost:
                    await send(body, execution_attempt_ref=ATTEMPT_REF)
                await asyncio.wait_for(peer.closed.wait(), 1)
                peer.closed.clear()
                self.assertTrue(lost.exception.attempt_mutation_may_have_committed)
                self.assertEqual(len(peer.requests), 1)
                result = await send(body, execution_attempt_ref=ATTEMPT_REF)
                await asyncio.wait_for(peer.closed.wait(), 1)
                self.assertTrue(result.replayed)
                self.assertEqual(peer.request_bodies, [body, body])
                signatures = []
                # silent-defaults: allow-contract -- The selected terminal method determines the expected complete or fail wire operation.
                operation = b"complete" if completing else b"fail"
                path = b"/provider/v1/execution-attempts/" + operation
                for request in peer.requests:
                    lines = request.split(b"\r\n")
                    self.assertEqual(lines[0], b"POST " + path + b" HTTP/1.1")
                    headers = {
                        name.lower(): value.strip() for line in lines[1:] if b":" in line for name, value in [line.split(b":", 1)]
                    }
                    self.assertEqual(parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0], sha256(body).digest())
                    base = b"\n".join(
                        [
                            b'"@method": POST',
                            b'"@authority": ' + peer.origin.removeprefix("https://").encode(),
                            b'"@path": ' + path,
                            b'"@query": ?',
                            b'"content-type": application/json',
                            b'"content-digest": ' + headers[b"content-digest"],
                            b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1],
                        ]
                    )
                    signature = parse(headers[b"signature"], tltype="dictionary")["sig1"][0]
                    key.public_key().verify(signature, base)
                    signatures.append(signature)
                self.assertNotEqual(*signatures)
                await transport.retire()

    async def test_cancellation_after_terminal_dispatch_keeps_owned_evidence(self):
        for completing in (True, False):
            body = (
                complete_body(ATTEMPT_REF, " result\n雪 ", b"result bytes")
                if completing
                else fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 ")
            )
            async with api_tls_peer("headers-stall") as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                api = ProviderAPI(
                    SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                    transport,
                    expected_topology="dev-local",
                )
                send = api.complete_attempt if completing else api.fail_attempt
                task = asyncio.create_task(send(body, execution_attempt_ref=ATTEMPT_REF))
                await asyncio.wait_for(peer.received.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(peer.closed.wait(), 1)
                self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
                self.assertIs(api.last_failure.cause, transport.last_failure)
                self.assertEqual(peer.request_bodies, [body])
                await transport.retire()
