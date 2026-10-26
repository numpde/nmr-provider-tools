"""Real signed TLS plus durable relay handoff, with recording scientific work."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_service import DeliveryBinding, DeliveryService
from provider_core.delivery_store import QueueLimits
from provider_core.execution import AttemptExecutor
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.starting import AttemptStarter
from provider_core.transport import HttpTransport
from tests.fakes.api import api
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response, observation, progress_response, start_response
from tests.fakes.conditions import ReportingSession
from tests.fakes.delivery import healthy_diagnostics
from tests.fakes.job_input import response


class ExecutionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_prepare_execute_handoff_and_separate_signed_publication_under_one_claim(self):
        replies = [start_response(), observation(), response(), progress_response(),
            progress_response(condition_code="waiting_capacity"), progress_response(), observation(),
            progress_response(phase="running"), observation(),
            progress_response(phase="running", condition_code="waiting_capacity"), progress_response(phase="running"),
            observation(), complete_response(result_schema_id="result.v1")]
        async with api_tls_peer("success", response_bodies=replies) as peer:
            selected_scope = replace(scope(), api_origin=peer.origin)
            pending = replace(record(), scope=selected_scope)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            sender = api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5), peer.origin)
            claim = AttemptClaims().claim_start(pending)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("control", "delivery", "ipc"):
                    (root / name).mkdir(mode=0o700)
                store = CheckpointStore(root / "control", scope=selected_scope, limits=CheckpointLimits(1, 150000), operation_seconds=1)
                await store.open()
                published = asyncio.Event()
                def notice(_error, facts):
                    if facts["event"] == "delivery_published":
                        published.set()
                endpoint = DeliveryEndpoint(peer.origin, selected_scope.provider_ref, root / "ipc/http.sock")
                relay = DeliveryService(root / "delivery", limits=QueueLimits(4, 16384, 8, 32768),
                    bindings=(DeliveryBinding(endpoint, sender),), request_seconds=1, retry_initial_seconds=0.01,
                    retry_max_seconds=0.02, on_notice=notice, diagnostic_status=healthy_diagnostics)
                relay_task = asyncio.create_task(relay.run())
                delivery = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
                session = ReportingSession(store)
                try:
                    await asyncio.wait_for(relay.ready.wait(), 1)
                    acknowledged = await AttemptStarter(client, store, claim).start(pending)
                    accepted = await AttemptExecutor(client, store, claim, session,
                        delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                        delivery_retry_max_seconds=0.02, allowed_job_states=frozenset({"open"}),
                        poll_seconds=1, task_settle_seconds=0.1).run(acknowledged)
                    self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                    self.assertEqual(await store.inventory(), (accepted,))
                    await asyncio.wait_for(published.wait(), 2)
                    self.assertEqual(relay.queue.inventory(peer.origin, selected_scope.provider_ref), [])
                    self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                    self.assertEqual(peer.request_bodies[0], pending.body)
                    self.assertEqual(accepted.selection.body_sha256, "sha256:" + hashlib.sha256(peer.request_bodies[-1]).hexdigest())
                    self.assertEqual(len(peer.requests), 13)
                    self.assertEqual(json.loads(peer.request_bodies[4])["condition_code"], "waiting_capacity")
                    self.assertEqual(json.loads(peer.request_bodies[9])["phase"], "running")
                    for request, body in zip(peer.requests[:-1], peer.request_bodies[:-1], strict=True):
                        verify_signed_request(request, body, origin=peer.origin, key=client.identity.private_key)
                    verify_signed_request(peer.requests[-1], peer.request_bodies[-1], origin=peer.origin, key=sender.identity.private_key)
                    self.assertTrue(peer.requests[-1].startswith(b"POST /provider/v1/execution-attempts/complete "))
                    self.assertNotIn(b"result bytes", (root / "control/checkpoints.sqlite3").read_bytes())
                    claim.release()
                finally:
                    relay.request_stop()
                    await asyncio.wait_for(relay_task, 2)
                    await delivery.retire()
                    await store.close()
            await transport.retire()
