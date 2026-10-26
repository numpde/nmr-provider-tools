"""The installed daemon command publishes through its explicit route and stops."""

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import complete_body
from provider_core.credential import parse_provider_credential
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.api import response_headers
from tests.fakes.credential import encoded
from tests.fakes.attempts import complete_response, fail_response
from tests.fakes.delivery import outcome
from tests.fakes.delivery_config import DeliveryConfigFiles
from tests.fakes.unix_proxy import UnixProxy


async def _launch_daemon(path):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-B", "-m", "provider_core.delivery_main", "--config", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def _receive_notice(process, event, notices):
    async with asyncio.timeout(3):
        while line := await process.stderr.readline():
            report = json.loads(line)
            notices.append(report)
            if report["event"] == event:
                return report
    raise AssertionError("Installed daemon exited before " + event)


def _configured_binding(root, peer, proxy, *, provider):
    """Reuse the explicit fixture document for an actual CLI binding and route."""
    root.mkdir(mode=0o700)
    files = DeliveryConfigFiles(root)
    (root / "state").mkdir(mode=0o700)
    (root / "ipc").mkdir(mode=0o710)
    credential = json.loads(files.key.read_bytes())
    credential.update(principal_ref=provider, credential_ref="credential:" + provider.split(":", 1)[1])
    files.key.write_bytes(encoded(credential))
    raw = files.raw.replace(b"https://api.example", peer.origin.encode())
    raw = raw.replace(b'provider_ref = "provider:example"', ("provider_ref = " + json.dumps(provider)).encode())
    raw = raw.replace(json.dumps(str(root / "api/http.sock")).encode(), json.dumps(proxy.path).encode())
    raw += b"ca_file = " + json.dumps(str(Path(peer.scratch.name) / "cert.pem")).encode() + b"\n"
    files.write(raw)
    return files, DeliveryEndpoint(peer.origin, provider, root / "ipc/http.sock"), parse_provider_credential(files.key.read_bytes())


async def _reap_daemon(process):
    """Retire the original child even when an assertion or client cleanup fails.

    Ports tests/fakes/runner.py::close's TERM/KILL and original-handle wait;
    communicate drains the two pipes while waiting for this same process.
    """
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), 3)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.communicate(), 3)


class DeliveryProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_discard_configuration_refusals_explain_the_reason_without_opening_queue_or_exposing_values(self):
        for schema, expected in (
            ("PRIVATE_SCHEMA", "Cannot inspect or discard held results: use the installed delivery configuration (schema provider-core.delivery.v1)."),
            ("provider-core.delivery.v1", "Cannot inspect or discard held results: the supplied API/provider pair must match exactly one configured binding."),
        ):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                queue = root / "PRIVATE_QUEUE"
                config = root / "PRIVATE_CONFIG.toml"
                config.write_text(
                    f'schema_id = "{schema}"\nbindings = []\nstate_directory = "{queue}"\n'
                    'private_canary = "PRIVATE_CONFIGURATION_VALUE"\n'
                )
                config.chmod(0o600)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-B", "-m", "provider_core.delivery_discard",
                    "--config", str(config), "--api-origin", "https://PRIVATE_ORIGIN.example",
                    "--provider-ref", "provider:PRIVATE_PROVIDER", "--list-held",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
                    self.assertEqual(process.returncode, 2)
                    self.assertEqual(stdout, b"")
                    self.assertIn(expected.encode(), stderr)
                    self.assertNotIn(b"PRIVATE", stderr)
                    self.assertNotIn(str(root).encode(), stderr)
                    self.assertFalse(queue.exists())
                finally:
                    await _reap_daemon(process)

    async def test_installed_command_delivers_once_through_proxy_then_sigterm_retires_it(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            async with UnixProxy(peer.origin, "tunnel") as proxy:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    files = DeliveryConfigFiles(root)
                    (root / "state").mkdir(mode=0o700)
                    (root / "ipc").mkdir(mode=0o710)
                    raw = files.raw.replace(b"https://api.example", peer.origin.encode())
                    raw = raw.replace(json.dumps(str(root / "api/http.sock")).encode(), json.dumps(proxy.path).encode())
                    raw += b"ca_file = " + json.dumps(str(Path(peer.scratch.name) / "cert.pem")).encode() + b"\n"
                    files.write(raw)
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-I", "-B", "-m", "provider_core.delivery_main", "--config", str(files.path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    endpoint = DeliveryEndpoint(peer.origin, "provider:example", root / "ipc/http.sock")
                    client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
                    notices = []

                    async def receive(event):
                        async with asyncio.timeout(3):
                            while line := await process.stderr.readline():
                                report = json.loads(line)
                                notices.append(report)
                                if report["event"] == event:
                                    return report
                        self.fail(f"Daemon exited before {event}.")

                    try:
                        await receive("delivery_ready")
                        self.assertEqual((root / "ipc").stat().st_mode & 0o7777, 0o710)
                        self.assertEqual((root / "ipc").stat().st_gid, os.getgid())
                        self.assertEqual(endpoint.socket_path.stat().st_mode & 0o777, 0o660)
                        self.assertEqual(endpoint.socket_path.stat().st_gid, os.getgid())
                        item = replace(outcome(provider="provider:example"), api_origin=peer.origin)
                        self.assertEqual(await client.submit(item), item.acceptance())
                        await receive("delivery_published")
                        self.assertEqual((await client.pending())["items"], [])
                        self.assertEqual(peer.request_bodies, [item.body])
                        self.assertEqual(len(proxy.headers), 1)
                        process.send_signal(signal.SIGTERM)
                        stopped = await receive("delivery_stopped")
                        self.assertTrue(stopped["retirement_confirmed"])
                        self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
                        self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", json.dumps(notices))
                        self.assertFalse(endpoint.socket_path.exists())
                        reopened = DeliveryQueue(root / "state", limits=QueueLimits(4, 16384, 8, 32768))
                        try:
                            self.assertEqual(reopened.inventory(item.api_origin, item.provider_ref), [])
                        finally:
                            reopened.close()
                    finally:
                        try:
                            await client.retire()
                        finally:
                            await _reap_daemon(process)

    async def test_bad_configuration_has_safe_receiving_diagnostics_and_no_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory)
            files.write(b"PRIVATE_CANARY malformed TOML")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", "-m", "provider_core.delivery_main", "--config", str(files.path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(stdout, b"")
                report = json.loads(stderr)
                self.assertEqual(report["event"], "delivery_failed")
                self.assertIn("UTF-8 TOML", json.dumps(report))
                self.assertNotIn("PRIVATE", stderr.decode())
                self.assertFalse((Path(directory) / "state").exists())
            finally:
                await _reap_daemon(process)

    async def test_lost_api_ack_then_actual_daemon_restart_replays_one_queued_outcome(self):
        """One scripted API effect survives a lost receipt; no producer is rerun.

        The peer remembers its synthetic effect. This proves daemon/queue replay,
        not a deployed API's transaction implementation or scientific correctness.
        """
        effects, received = {}, []
        item = credential = None

        async def accept_then_lose(request, body):
            self.assertEqual(request.split(b"\r\n", 1)[0], b"POST /provider/v1/execution-attempts/complete HTTP/1.1")
            self.assertEqual(body, item.body)
            verify_signed_request(request, body, origin=peer.origin, key=credential.private_key)
            received.append(request)
            replayed = item.key in effects
            if replayed:
                self.assertEqual(effects[item.key], body)
            else:
                effects[item.key] = body
                return None  # Full body received; lose only this API acknowledgement.
            return complete_response(execution_attempt_ref=item.execution_attempt_ref,
                                     result_schema_id="fixture.result", replayed=True)

        async with api_tls_peer("success", response_factory=accept_then_lose) as peer:
            async with UnixProxy(peer.origin, "tunnel") as proxy:
                with tempfile.TemporaryDirectory() as directory:
                    files, endpoint, credential = _configured_binding(Path(directory) / "binding", peer, proxy,
                        provider="provider:example")
                    # The minimum jittered retry wait is 15s, longer than bounded
                    # receipt observation and original-process reaping below.
                    files.write(files.path.read_bytes().replace(b"retry_initial_seconds = 0.01", b"retry_initial_seconds = 30")
                        .replace(b"retry_max_seconds = 0.1", b"retry_max_seconds = 30"))
                    configured = files.path.read_bytes()
                    item = replace(outcome(provider=endpoint.provider_ref), api_origin=peer.origin)
                    item = replace(item, operation=TerminalOperation.COMPLETE,
                                   body=complete_body(item.execution_attempt_ref, "fixture.result", b"result bytes") + b"\n")
                    client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
                    first = second = None
                    notices = []
                    try:
                        first = await _launch_daemon(files.path)
                        await _receive_notice(first, "delivery_ready", notices)
                        self.assertEqual(await client.submit(item), item.acceptance())
                        retry = await _receive_notice(first, "delivery_retry", notices)
                        self.assertGreaterEqual(retry["retry_seconds"], 15)
                        self.assertLessEqual(retry["retry_seconds"], 30)
                        await client.retire()
                        first.kill()
                        await asyncio.wait_for(first.communicate(), 3)
                        self.assertEqual(first.returncode, -signal.SIGKILL)
                        retained = DeliveryQueue(files.root / "state", limits=QueueLimits(4, 16384, 8, 32768))
                        try:
                            self.assertEqual(retained.next_pending(item.api_origin, item.provider_ref), item)
                            self.assertEqual(len(retained.inventory(item.api_origin, item.provider_ref)), 1)
                        finally:
                            retained.close()
                        self.assertEqual(peer.request_bodies, [item.body])
                        self.assertEqual(files.path.read_bytes(), configured)
                        second = await _launch_daemon(files.path)
                        await _receive_notice(second, "delivery_ready", notices)
                        published = await _receive_notice(second, "delivery_published", notices)
                        self.assertTrue(published["replayed"])
                        self.assertEqual((await client.pending())["items"], [])
                        self.assertEqual(peer.request_bodies, [item.body, item.body])
                        self.assertEqual(effects, {item.key: item.body})
                        signatures = [request.split(b"Signature-Input: ")[1].split(b"\r\n")[0] for request in received]
                        self.assertNotEqual(signatures[0], signatures[1])
                        self.assertNotIn("cmVzdWx0IGJ5dGVz", json.dumps(notices))
                        second.send_signal(signal.SIGTERM)
                        self.assertTrue((await _receive_notice(second, "delivery_stopped", notices))["retirement_confirmed"])
                        self.assertEqual(await asyncio.wait_for(second.wait(), 3), 0)
                    finally:
                        try:
                            await client.retire()
                        finally:
                            for process in (first, second):
                                if process is not None:
                                    await _reap_daemon(process)

    async def test_two_bindings_isolate_grants_and_publish_while_one_api_is_unavailable(self):
        recover = asyncio.Event()
        effects = {"a": 0, "b": 0}
        first = second = credential_a = credential_b = None

        async def api_a(request, body):
            self.assertEqual(request.split(b"\r\n", 1)[0], b"POST /provider/v1/execution-attempts/fail HTTP/1.1")
            self.assertEqual(body, first.body)
            verify_signed_request(request, body, origin=peer_a.origin, key=credential_a.private_key)
            if not recover.is_set():
                peer_a.response_status = 503
                peer_a.response_headers = response_headers(status=503, request_id=b"request:api-a-outage")
                return json.dumps({"type": "urn:nmr-api:problem:service-unavailable", "title": "Service unavailable",
                    "status": 503, "instance": "urn:nmr-api:request:api-a-outage",
                    "request_id": "request:api-a-outage", "code": "service_unavailable"}).encode()
            peer_a.response_status = 200
            peer_a.response_headers = response_headers()
            effects["a"] += 1
            return fail_response(execution_attempt_ref=first.execution_attempt_ref,
                failure_code="provider_interrupted", failure_message="PRIVATE_BINDING_A_PAYLOAD")

        async def api_b(request, body):
            self.assertEqual(request.split(b"\r\n", 1)[0], b"POST /provider/v1/execution-attempts/fail HTTP/1.1")
            self.assertEqual(body, second.body)
            verify_signed_request(request, body, origin=peer_b.origin, key=credential_b.private_key)
            effects["b"] += 1
            return fail_response(execution_attempt_ref=second.execution_attempt_ref,
                failure_code="provider_interrupted", failure_message="PRIVATE_BINDING_B_PAYLOAD")

        async with api_tls_peer("success", response_factory=api_a) as peer_a, api_tls_peer("success", response_factory=api_b) as peer_b:
            async with UnixProxy(peer_a.origin, "tunnel") as proxy_a, UnixProxy(peer_b.origin, "tunnel") as proxy_b:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    files, endpoint_a, credential_a = _configured_binding(root / "first", peer_a, proxy_a, provider="provider:example")
                    other, endpoint_b, credential_b = _configured_binding(root / "second", peer_b, proxy_b, provider="provider:other")
                    self.assertNotEqual(credential_a.private_key.public_key().public_bytes_raw(),
                                        credential_b.private_key.public_key().public_bytes_raw())
                    files.write(files.path.read_bytes() + b"[[bindings]]" + other.path.read_bytes().split(b"[[bindings]]", 1)[1])
                    first = replace(outcome(provider=endpoint_a.provider_ref, message="PRIVATE_BINDING_A_PAYLOAD"), api_origin=peer_a.origin)
                    second = replace(outcome(provider=endpoint_b.provider_ref, message="PRIVATE_BINDING_B_PAYLOAD"), api_origin=peer_b.origin)
                    clients = [DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2) for endpoint in (endpoint_a, endpoint_b)]
                    foreign = DeliveryClient(replace(endpoint_b, socket_path=endpoint_a.socket_path), exchange_seconds=1, retirement_seconds=0.2)
                    process, notices = await _launch_daemon(files.path), []
                    try:
                        self.assertEqual((await _receive_notice(process, "delivery_ready", notices))["binding_count"], 2)
                        with self.assertRaises(DeliveryRejected):
                            await foreign.submit(second)
                        self.assertEqual((await clients[0].pending())["items"], [])
                        self.assertEqual((await clients[1].pending())["items"], [])
                        self.assertEqual(peer_a.requests, [])
                        self.assertEqual(peer_b.requests, [])
                        self.assertEqual(effects, {"a": 0, "b": 0})
                        self.assertEqual(await clients[0].submit(first), first.acceptance())
                        outage = await _receive_notice(process, "delivery_retry", notices)
                        self.assertEqual((outage["api_origin"], outage["provider_ref"]), (peer_a.origin, endpoint_a.provider_ref))
                        self.assertEqual(await clients[1].submit(second), second.acceptance())
                        healthy = await _receive_notice(process, "delivery_published", notices)
                        self.assertEqual((healthy["api_origin"], healthy["provider_ref"]), (peer_b.origin, endpoint_b.provider_ref))
                        self.assertIsNone(process.returncode)
                        pending_a = await clients[0].pending()
                        self.assertEqual([(row["body_sha256"], row["state"]) for row in pending_a["items"]], [(first.body_sha256, "pending")])
                        self.assertEqual((await clients[1].pending())["items"], [])
                        self.assertEqual(peer_b.request_bodies, [second.body])
                        self.assertEqual(effects, {"a": 0, "b": 1})
                        recover.set()
                        recovered = await _receive_notice(process, "delivery_published", notices)
                        self.assertEqual((recovered["api_origin"], recovered["provider_ref"]), (peer_a.origin, endpoint_a.provider_ref))
                        self.assertEqual((await clients[0].pending())["items"], [])
                        self.assertGreaterEqual(len(peer_a.request_bodies), 2)
                        self.assertTrue(all(body == first.body for body in peer_a.request_bodies))
                        self.assertEqual(peer_b.request_bodies, [second.body])
                        self.assertEqual(effects, {"a": 1, "b": 1})
                        self.assertIsNone(process.returncode)
                        for private in ("PRIVATE_BINDING_A_PAYLOAD", "PRIVATE_BINDING_B_PAYLOAD"):
                            self.assertNotIn(private, json.dumps(notices))
                        process.send_signal(signal.SIGTERM)
                        self.assertTrue((await _receive_notice(process, "delivery_stopped", notices))["retirement_confirmed"])
                        self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
                    finally:
                        try:
                            for client in (*clients, foreign):
                                await client.retire()
                        finally:
                            await _reap_daemon(process)
