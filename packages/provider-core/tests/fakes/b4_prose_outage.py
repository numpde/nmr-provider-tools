"""Exercise a sustained prose-service outage with real SDK owners and TLS peers.

This is an SDK test driver, not a replacement for a provider's discovery loop.
It reuses the B3 counting scientific worker and actual durable outbox/publisher.
The local relay connection alone is replaced by the queue's durable enqueue.
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
import random
from unittest.mock import Mock

from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence, StartPending
from provider_core.attempt_request import read_complete_body, read_fail_body
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.conversation import ToolFinished
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.delivery_worker import DeliveryWorker
from provider_core.diagnostics import failure_evidence
from provider_core.interpreter import InterpretationFailed, Interpreter, ProseService
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.transport import HttpTransport
from tests.fakes.api import Transport, api, reply
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response, fail_response, observation, progress_response, retained_start, start_response
from tests.fakes.b3_repetition_fault import _descriptors, _process_alive, _settled_peer_handlers
from tests.fakes.chat_process_fault import WarmWorker
from tests.fakes.conversation import call
from tests.fakes.job_input import response
from tests.fakes.tls_peer import TlsPeer


class ObservedFactory:
    """Keep the actual HTTP carriers for pre-teardown resource observations."""

    def __init__(self, factory):
        self.factory, self.endpoints = factory, []

    @property
    def context_bytes(self):
        return self.factory.context_bytes

    def __call__(self, *, exchange_seconds, connect_seconds):
        endpoint = self.factory(exchange_seconds=exchange_seconds, connect_seconds=connect_seconds)
        self.endpoints.append(endpoint)
        return endpoint


class JobsControlTransport(Transport):
    """Correlated Job control; publication uses a separately authenticated TLS peer."""

    def __init__(self, job_ref):
        super().__init__()
        self.job_ref = job_ref
        self.attempt_ref = "execution_attempt:sha256:" + sha256(job_ref.encode()).hexdigest()
        self.start_times = []

    async def exchange(self, request, *, maximum_bytes, owner=None):
        self.calls.append((request, maximum_bytes))
        if request.url.path.endswith("/start"):
            self.start_times.append(asyncio.get_running_loop().time())
            return reply(start_response(job_ref=self.job_ref, execution_attempt_ref=self.attempt_ref))
        if request.url.path.endswith("/progress"):
            sent = json.loads(request.content)
            return reply(progress_response(execution_attempt_ref=self.attempt_ref,
                                           phase=sent["phase"], condition_code=sent["condition_code"]))
        if request.method == "GET":
            return reply(observation(job_ref=self.job_ref, execution_attempt_ref=self.attempt_ref))
        raise AssertionError("Unexpected Job control request in the outage fixture")


class DurableHandoff:
    """Acknowledge actual SQLite custody; the publisher still owns API publication."""

    def __init__(self, inbox, endpoint, publisher):
        self.inbox, self.endpoint, self.publisher = inbox, endpoint, publisher
        self.items = []
        self.retirement_confirmed, self.last_failure = True, None

    async def submit(self, item):
        self.items.append(item)
        accepted = await self.inbox.storage(self.inbox.queue.enqueue, item)
        self.publisher.wake()
        return accepted

    async def retire(self):
        self.retirement_confirmed = True

    def can_retry(self, failure):
        return False


async def run_outage(directory, *, mode, backlog, seed):
    """Return measurements before closing peers, publisher or scientific worker."""
    loop = asyncio.get_running_loop()
    random_order = random.Random(seed)
    # All model requests include fresh Python startup, TLS and tool decoding.
    # The unavailable peer must outlast the affected original deadline; the
    # independent healthy route must have enough allowance to finish science.
    outage_seconds = 5.0 if mode == "blocked" else 4.0
    interpretation_seconds = 2.0 if mode == "blocked" else 1.5
    healthy_interpretation_seconds = 4.0
    retirement_seconds, recovery_seconds = 0.3, 5
    minimum_delay = 1 if mode == "retry_after" else 0.25
    request_ceiling = (1 + math.floor(outage_seconds / interpretation_seconds)
                       if mode == "blocked" else 1 + math.floor(outage_seconds / minimum_delay))
    maximum_attempts = 2
    receipt_release_phase = random_order.choice(("request_received", "failed_job_settled"))
    claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=maximum_attempts))
    stopping, restored, first_failed_request, first_failed_job = (asyncio.Event() for _ in range(4))
    original_report_received, release_original_receipt = asyncio.Event(), asyncio.Event()
    trace, requests, jobs, notices, publication_receipts = [], [], [], [], []
    published = {}
    scientific_worker, worker_lock = WarmWorker(), asyncio.Lock()
    original_report_ref, restored_at = None, None
    peak_claims, peak_prose_connections = 0, 0

    def event(name, **facts):
        trace.append({"event": name, "at": loop.time(), **facts})

    async def science(operation, job=None):
        async with worker_lock:
            return await scientific_worker.call(operation, job)

    successful_reply = json.dumps({"choices": [{
        "message": call("prepare", "prepare_scientific_input", {}), "finish_reason": "tool_calls",
    }]}).encode()

    def failed_service_mode(_request, body):
        nonlocal peak_prose_connections
        job = json.loads(body)["messages"][0]["content"]
        available = restored.is_set()
        requests.append({"job": job, "at": loop.time(), "available": available})
        peak_prose_connections = max(peak_prose_connections, sum(not task.done() for task in failed_peer.handlers))
        if not available:
            first_failed_request.set()
        return "headers-stall" if mode == "blocked" and not available else "success"

    async def failed_service_response(_request, _body):
        if restored.is_set():
            failed_peer.response_status, failed_peer.response_headers = 200, None
            return successful_reply
        failed_peer.response_status = 503
        headers = ((b"Content-Type", b"application/json"),)
        if mode == "retry_after":
            headers += ((b"Retry-After", b"1"),)
        elif mode == "invalid_retry_after":
            headers += ((b"Retry-After", b"not-a-date"),)
        failed_peer.response_headers = headers
        return None if mode == "disconnect" else b'{"error":{"message":"The prose service is unavailable."}}'

    async def jobs_api_response(request, body):
        reference = json.loads(body)["execution_attempt_ref"]
        if request.split(b" ")[1].endswith(b"/complete"):
            schema, content = read_complete_body(body, execution_attempt_ref=reference)
            if reference == original_report_ref:
                original_report_received.set()
                event("original_report_received_by_jobs_api")
                await release_original_receipt.wait()
            receipt = complete_response(
                execution_attempt_ref=reference, result_schema_id=schema,
                analysis_result_ref="analysis_result:sha256:" + sha256(body).hexdigest(),
                result_fingerprint="sha256:" + sha256(content).hexdigest(), result_byte_length=len(content),
            )
        else:
            code, message = read_fail_body(body, execution_attempt_ref=reference)
            receipt = fail_response(execution_attempt_ref=reference, failure_code=code, failure_message=message)
        publication_receipts.append(receipt)
        return receipt

    def delivery_notice(error, facts):
        notices.append({"error": None if error is None else type(error).__name__, **facts})
        if facts["event"] == "delivery_published":
            published[facts["execution_attempt_ref"]] = loop.time()

    class ScientificSession:
        def __init__(self, job_ref, service):
            self.job_ref = job_ref
            self.prepare_calls = self.scientific_prepare_calls = self.execute_calls = 0
            self.handle = self.failure = None
            self.retired = False
            # Healthy reporting and deliberately blocked model requests have
            # separate, explicit fixed allowances in this fixture.
            model_seconds = (healthy_interpretation_seconds if
                             job_ref.endswith(("original-result", "healthy-during-outage")) else interpretation_seconds)
            self.interpreter = Interpreter((service,), timeout_seconds=model_seconds,
                                           context_bytes=16384, secrets=("B4_TEST_CREDENTIAL",))

        async def prepare(self, acknowledged, instructions, *, report_condition):
            nonlocal peak_claims
            peak_claims = max(peak_claims, len(claims.live_owners()))
            self.prepare_calls += 1
            if acknowledged.start.input != instructions.identity:
                raise AssertionError("The original Job's input changed during preparation")

            async def dispatch(name, arguments, *, not_after):
                if name != "prepare_scientific_input" or arguments:
                    raise AssertionError("The prose service proposed an unexpected scientific operation")
                report_condition.require_execution()
                self.scientific_prepare_calls += 1
                await science("prepare", self.job_ref)
                self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", self.job_ref.encode()))
                return ToolFinished(self.handle)

            tools = [{"type": "function", "function": {
                "name": "prepare_scientific_input", "parameters": {"type": "object", "properties": {}},
            }}]
            try:
                return (await self.interpreter.run([{"role": "user", "content": self.job_ref}], tools, dispatch)).value
            except InterpretationFailed as failure:
                self.failure = failure
                return AnalysisFailure(
                    "interpreter_unavailable", "The prose service was unavailable within this Job's interpretation deadline.",
                    ProtectedEvidence("fixture.interpreter.failure.v1", b"original interpreter failure retained"),
                )

        async def execute(self, prepared, *, report_condition):
            if prepared is not self.handle or prepared is None:
                raise AssertionError("Scientific execution changed its original prepared handle")
            report_condition.require_execution()
            self.execute_calls += 1
            value = await science("execute", self.job_ref)
            self.handle = None
            return AnalysisSuccess("fixture.counted_science.v1", json.dumps(value).encode(),
                                   ProtectedEvidence("fixture.science.evidence.v1", b"one scientific execution"))

        async def retire(self):
            await self.interpreter.retire()
            if not self.interpreter.retirement_confirmed:
                raise AssertionError("The original prose HTTP exchange has not retired")
            if not self.retired:
                await science("retire", self.job_ref)
                self.retired = True

    async with AsyncExitStack() as stack:
        failed_peer = await stack.enter_async_context(TlsPeer(
            "success", response_factory=failed_service_response, response_mode=failed_service_mode,
        ))
        healthy_peer = await stack.enter_async_context(TlsPeer("success", response_body=successful_reply))
        jobs_peer = await stack.enter_async_context(api_tls_peer("success", response_factory=jobs_api_response))
        observed_factories = tuple(ObservedFactory(ChatEndpointFactory(
            spec=ChatEndpointSpec(configuration_id=name, model="local-prose-test-model",
                                  url=peer.origin + "/chat", api_key="B4_TEST_CREDENTIAL", reasoning_effort=None),
            trust=peer.trust, retirement_seconds=retirement_seconds, request_bytes=32768, response_bytes=8192,
        )) for name, peer in (("unavailable-prose-service", failed_peer), ("independent-prose-service", healthy_peer)))
        failed_service, healthy_service = (ProseService(factory) for factory in observed_factories)
        directory.mkdir(mode=0o700)
        queue = DeliveryQueue(directory, limits=QueueLimits(backlog + 8, 1048576, backlog + 8, 1048576))
        inbox = DeliveryInbox(queue, (), request_seconds=1, on_accepted=Mock(), on_failure=Mock(), binding_status=Mock())
        selected_scope = replace(scope(), api_origin=jobs_peer.origin)
        endpoint = DeliveryEndpoint(selected_scope.api_origin, selected_scope.provider_ref, directory / "unused-relay.sock")
        delivery_transport = HttpTransport(trust=jobs_peer.trust, exchange_seconds=8, retirement_seconds=retirement_seconds)
        delivery_api = api(delivery_transport, jobs_peer.origin)
        publisher = DeliveryWorker(inbox, endpoint, delivery_api, retry_initial_seconds=0.01,
                                   retry_max_seconds=0.04, on_notice=delivery_notice)
        local_delivery = DurableHandoff(inbox, endpoint, publisher)
        tasks = []
        await scientific_worker.start()
        publisher_task = asyncio.create_task(publisher.run())

        async def run_job(name, service):
            job_ref = "job:b4-" + name
            base = record("b4:" + name)
            pending = StartPending(selected_scope, base.analysis_kind_ref, replace(base.input, job_ref=job_ref),
                                   retained_start(job_ref=job_ref, provider_attempt_key="b4:" + name))
            session, control = ScientificSession(job_ref, service), JobsControlTransport(job_ref)
            controller = AttemptController.live(
                api(control, selected_scope.api_origin), claims, session, scope=selected_scope,
                stopping=stopping, start_policy=LiveStartPolicy(0.01, Mock(), Mock()),
                preverified_input=verify_job_input(response(job_ref=job_ref), feed=pending.input),
                delivery=local_delivery, on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
                delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                poll_seconds=1, task_settle_seconds=retirement_seconds,
            )
            await controller.run(pending)
            job = {"name": name, "attempt_ref": control.attempt_ref,
                   "prepare_calls": session.prepare_calls, "scientific_prepare_calls": session.scientific_prepare_calls,
                   "execute_calls": session.execute_calls, "failed": session.failure is not None,
                   "starts": sum(request.url.path.endswith("/start") for request, _ in control.calls),
                   "shutdown_ready": controller.shutdown_ready, "started_at": control.start_times[0], "finished_at": loop.time(),
                   "diagnostic": None if session.failure is None else failure_evidence(session.failure, secrets=("B4_TEST_CREDENTIAL",))}
            jobs.append(job)
            if name.startswith("affected-") and session.failure is not None:
                first_failed_job.set()
            return job

        async def wait_for_publication(references):
            async with asyncio.timeout(3):
                while not references <= published.keys():
                    await asyncio.sleep(0.005)

        async def restore_service():
            nonlocal restored_at
            await first_failed_request.wait()
            event("outage_window_started")
            await asyncio.sleep(outage_seconds)
            restored_at = loop.time()
            restored.set()
            event("prose_service_restored")

        async def drive_backlog():
            # This deliberately simple SDK driver cannot prove a production
            # provider's discovery/admission policy. Every name is used once.
            for number in range(backlog):
                await run_job("affected-" + str(number), failed_service)

        async def healthy_work_and_original_receipt():
            await first_failed_request.wait()
            healthy = await run_job("healthy-during-outage", healthy_service)
            if receipt_release_phase == "failed_job_settled":
                await first_failed_job.wait()
            event("original_jobs_api_receipt_released", publisher_owns_request=delivery_transport.pending_owner is publisher)
            release_original_receipt.set()
            await wait_for_publication({original_report_ref, healthy["attempt_ref"]})
            event("healthy_report_published_during_outage", prose_service_still_unavailable=not restored.is_set())

        try:
            idle_descriptors = _descriptors()
            original_report_ref = JobsControlTransport("job:b4-original-result").attempt_ref
            await run_job("original-result", healthy_service)
            await asyncio.wait_for(original_report_received.wait(), 8)
            original_report = local_delivery.items[0]
            tasks = [asyncio.create_task(restore_service()), asyncio.create_task(drive_backlog()),
                     asyncio.create_task(healthy_work_and_original_receipt())]
            await asyncio.wait_for(asyncio.gather(*tasks), backlog * (interpretation_seconds + retirement_seconds) + 8)
            await wait_for_publication({item.execution_attempt_ref for item in local_delivery.items})
            peer_connections = [await _settled_peer_handlers(peer) for peer in (failed_peer, healthy_peer, jobs_peer)]
            for request, body in zip(jobs_peer.requests, jobs_peer.request_bodies, strict=True):
                verify_signed_request(request, body, origin=jobs_peer.origin, key=delivery_api.identity.private_key)
            carriers = [endpoint for factory in observed_factories for endpoint in factory.endpoints]
            children = [endpoint._transport.last_process_result for endpoint in carriers
                        if endpoint._transport.last_process_result is not None]
            return {
                "mode": mode, "seed": seed, "backlog": backlog, "maximum_attempts": maximum_attempts,
                "outage_seconds": outage_seconds, "request_ceiling": request_ceiling,
                "minimum_request_delay": minimum_delay, "recovery_seconds": recovery_seconds,
                "receipt_release_phase": receipt_release_phase, "requests": requests, "trace": trace, "jobs": jobs,
                "restored_at": restored_at, "publication_times": published, "notices": notices,
                "peak_claims": peak_claims, "peak_prose_connections": peak_prose_connections,
                "provider_pid": os.getpid(), "scientific_worker_before": scientific_worker.initial,
                "scientific_worker_after": await science("snapshot"),
                "original_report_acceptance": original_report.acceptance(),
                "original_report_bytes_sent_once": jobs_peer.request_bodies.count(original_report.body) == 1,
                "original_report_receipt": json.loads(publication_receipts[0]),
                "original_report_sha256": sha256(original_report.body).hexdigest(),
                "submitted_reports": len(local_delivery.items), "published_reports": len(published),
                "all_report_bytes_preserved": sorted(item.body for item in local_delivery.items) == sorted(jobs_peer.request_bodies),
                "publisher_running": not publisher_task.done(),
                "queue_inventory": queue.inventory(selected_scope.api_origin, selected_scope.provider_ref),
                "final_claims": len(claims.live_owners()), "open_peer_connections": peer_connections,
                "all_http_owners_retired": all(endpoint.retirement_confirmed for endpoint in carriers),
                "children": [{"pid": child.pid, "returncode": child.returncode, "alive": _process_alive(child.pid)}
                             for child in children],
                "idle_descriptors": idle_descriptors, "final_descriptors": _descriptors(),
            }
        finally:
            release_original_receipt.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            publisher.request_stop()
            await asyncio.wait_for(publisher_task, 3)
            await inbox.close()
            queue.close()
            scientific_worker.process.stdin.close()
            await asyncio.wait_for(scientific_worker.process.wait(), 3)
