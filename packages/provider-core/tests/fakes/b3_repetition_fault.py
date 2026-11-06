"""Repeat real interpreter HTTP failures while one original result awaits the Jobs API.

The provider fixture uses the SDK controller, interpreter, SQLite delivery queue,
publisher and signed API client. Its scientific worker is B1's persistent counting
process. Only the local relay connection is replaced by direct durable enqueue;
that acknowledgement is deliberately distinct from the later Jobs API receipt.
"""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import signal
import sys
import time
import traceback
from unittest.mock import Mock, patch


def _process_alive(pid):
    try:
        os.kill(pid, 0)
    # silent-defaults: allow-exception-outcome -- ESRCH establishes that the previously observed child PID no longer exists.
    except ProcessLookupError:
        return False
    return True


def _descriptors():
    """Count sockets and pipes in the still-running Linux provider fixture."""
    targets = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            targets.append(str(descriptor.readlink()))
        # silent-defaults: allow-contract -- A descriptor closed during enumeration is no longer an owned resource.
        except FileNotFoundError:
            pass
    return {kind: sum(target.startswith(kind + ":[") for target in targets) for kind in ("socket", "pipe")}


async def _settled_peer_handlers(peer):
    """Observe each connection through writer.wait_closed; never close it here."""
    active = {task for task in peer.handlers if not task.done()}
    if active:
        await asyncio.wait(active, timeout=1)
    return sum(not task.done() for task in peer.handlers)


async def scenario(marker, report, seed):
    from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
    from provider_core.attempt_record import ProtectedEvidence, StartPending
    from provider_core.attempt_request import read_complete_body, read_fail_body
    from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
    from provider_core.chat_process import SupervisedChatTransport
    from provider_core.controller import AttemptController, LiveStartPolicy
    from provider_core.conversation import ToolFinished
    from provider_core.delivery_client import DeliveryEndpoint
    from provider_core.delivery_ipc import DeliveryInbox
    from provider_core.delivery_store import DeliveryQueue, QueueLimits
    from provider_core.delivery_worker import DeliveryWorker
    from provider_core.diagnostics import failure_evidence
    from provider_core.interpreter import InterpretationFailed, Interpreter
    from provider_core.job_input import verify_job_input
    from provider_core.ownership import AttemptClaims, LiveAttemptLimits
    from provider_core.transport import HttpTransport
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.api_tls import api_tls_peer, verify_signed_request
    from tests.fakes.attempt_store import record, scope
    from tests.fakes.attempts import complete_response, fail_response, observation, progress_response, retained_start, start_response
    from tests.fakes.chat_process_fault import WarmWorker, _failure_types
    from tests.fakes.conversation import call
    from tests.fakes.job_input import response
    from tests.fakes.tls_peer import TlsPeer

    random_order = random.Random(seed)
    acknowledgement_cycle = random_order.randrange(2, 5)
    acknowledgement_phase = random_order.choice(("request_received", "cleanup_engaged"))
    release_api_receipt = asyncio.Event()
    completed_result_received = asyncio.Event()
    published = set()
    notices, cycles, jobs, first_http_stops, completion_receipts = [], [], [], {}, []
    result = {"seed": seed, "acknowledgement_cycle": acknowledgement_cycle,
              "acknowledgement_phase": acknowledgement_phase, "cycles": cycles}
    maximum_attempts, repetitions = 2, 5
    claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=maximum_attempts))
    provider_stopping = asyncio.Event()
    scientific_worker = WarmWorker()
    await scientific_worker.start()

    class JobsControlTransport(Transport):
        """Correlated START/progress/read replies; publication uses real HTTPS."""

        def __init__(self, job_ref):
            super().__init__()
            self.job_ref = job_ref
            self.attempt_ref = "execution_attempt:sha256:" + sha256(job_ref.encode()).hexdigest()

        async def exchange(self, request, *, maximum_bytes, owner=None):
            self.calls.append((request, maximum_bytes))
            if request.url.path.endswith("/start"):
                return reply(start_response(job_ref=self.job_ref, execution_attempt_ref=self.attempt_ref))
            if request.url.path.endswith("/progress"):
                sent = json.loads(request.content)
                return reply(progress_response(execution_attempt_ref=self.attempt_ref,
                                               phase=sent["phase"], condition_code=sent["condition_code"]))
            if request.method == "GET":
                return reply(observation(job_ref=self.job_ref, execution_attempt_ref=self.attempt_ref))
            raise AssertionError("Unexpected Job control request")

    class ScientificSession:
        def __init__(self, job_ref, url, trust, scientific_worker):
            self.job_ref, self.scientific_worker = job_ref, scientific_worker
            self.prepare_calls = self.execute_calls = self.scientific_prepare_calls = 0
            self.retired = False
            self.handle = self.failure = None
            self.http_endpoints = []
            factory = ChatEndpointFactory(
                spec=ChatEndpointSpec(configuration_id="b3-prose-interpreter", model="b3-fixture-prose-interpreter",
                                      url=url, api_key="PRIVATE_B3_FIXTURE_KEY", reasoning_effort=None),
                trust=trust, retirement_seconds=0.3, request_bytes=32768, response_bytes=8192,
            )

            def capture_endpoint(*, exchange_seconds, connect_seconds):
                endpoint = factory(exchange_seconds=exchange_seconds, connect_seconds=connect_seconds)
                self.http_endpoints.append(endpoint)
                return endpoint

            # /ready needs startup, TLS and scientific tool acceptance.
            # /hang must enter the blocked read and expire within its own
            # original finite model deadline.
            timeout_seconds = 4.0
            if url.endswith("/hang"):
                timeout_seconds = 2.0
            self.interpreter = Interpreter((capture_endpoint,), timeout_seconds=timeout_seconds, context_bytes=16384,
                                           secrets=("PRIVATE_B3_FIXTURE_KEY",))

        async def prepare(self, acknowledged, instructions, *, report_condition):
            self.prepare_calls += 1
            if acknowledged.start.input != instructions.identity:
                raise AssertionError("Session preparation changed its original Job input")

            async def dispatch(name, arguments, *, not_after):
                if name != "prepare_scientific_input" or arguments:
                    raise AssertionError("Unexpected prose-interpreter tool request")
                report_condition.require_execution()
                self.scientific_prepare_calls += 1
                await self.scientific_worker.call("prepare", self.job_ref)
                self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", self.job_ref.encode()))
                return ToolFinished(self.handle)

            tools = [{"type": "function", "function": {
                "name": "prepare_scientific_input", "parameters": {"type": "object", "properties": {}}}}]
            try:
                prepared = await self.interpreter.run([], tools, dispatch)
            except InterpretationFailed as failure:
                self.failure = failure
                unavailable = AnalysisFailure(
                    "interpreter_failed", "The prose-interpretation HTTPS request exceeded its original deadline.",
                    ProtectedEvidence("fixture.interpreter.failure.v1", b"original timeout retained by session"),
                )
            return unavailable
            return prepared.value

        async def execute(self, prepared, *, report_condition):
            if prepared is not self.handle or prepared is None:
                raise AssertionError("Scientific execution changed its original prepared handle")
            report_condition.require_execution()
            self.execute_calls += 1
            value = await self.scientific_worker.call("execute", self.job_ref)
            self.handle = None
            return AnalysisSuccess("fixture.counted_science.v1", json.dumps(value).encode(),
                                   ProtectedEvidence("fixture.science.evidence.v1", b"one scientific execution"))

        async def retire(self):
            await self.interpreter.retire()
            if not self.interpreter.retirement_confirmed:
                raise AssertionError("The original prose-interpreter HTTP child has not retired")
            if not self.retired:
                await self.scientific_worker.call("retire", self.job_ref)
                self.retired = True

    class DirectDurableHandoff:
        """Replace local Unix IPC only; return the production queue's exact receipt."""

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

    async def jobs_api_response(request, body):
        document = json.loads(body)
        reference = document["execution_attempt_ref"]
        if request.split(b" ")[1].endswith(b"/complete"):
            schema, content = read_complete_body(body, execution_attempt_ref=reference)
            if not completed_result_received.is_set():
                completed_result_received.set()
                await release_api_receipt.wait()
            return complete_response(
                execution_attempt_ref=reference, result_schema_id=schema,
                analysis_result_ref="analysis_result:sha256:" + sha256(body).hexdigest(),
                result_fingerprint="sha256:" + sha256(content).hexdigest(), result_byte_length=len(content),
            )
        code, message = read_fail_body(body, execution_attempt_ref=reference)
        return fail_response(execution_attempt_ref=reference, failure_code=code, failure_message=message)

    def prose_response_mode(request, _body):
        if request.split(b" ")[1] == b"/hang":
            return "headers-stall"
        return "success"

    def notice(error, facts):
        reason = None
        if error is not None:
            reason = type(error).__name__
        notices.append((reason, facts))
        if facts["event"] == "delivery_published":
            published.add(facts["execution_attempt_ref"])

    original_stop = SupervisedChatTransport._stop

    def record_first_http_stop(transport, exchange, cause, *, at=None):
        if exchange.stopped_at is None:
            first_http_stops[transport] = cause
        return original_stop(transport, exchange, cause, at=at)

    response_body = json.dumps({"choices": [{"message": call("prepared", "prepare_scientific_input", {}),
                                            "finish_reason": "tool_calls"}]}).encode()
    with ExitStack() as patches:
        command = (sys.executable, "-I", "-B", str(Path(__file__).resolve()), "http-worker", str(marker))
        patches.enter_context(patch("provider_core.chat_process._worker_command", return_value=command))
        patches.enter_context(patch.object(SupervisedChatTransport, "_stop", new=record_first_http_stop))
        async with TlsPeer("success", response_body=response_body, response_mode=prose_response_mode) as prose_peer:
            async with api_tls_peer("success", response_factory=jobs_api_response) as jobs_api_peer:
                delivery_directory = report.parent / "delivery-state"
                delivery_directory.mkdir(mode=0o700)
                queue = DeliveryQueue(delivery_directory, limits=QueueLimits(16, 131072, 16, 131072))
                inbox = DeliveryInbox(queue, (), request_seconds=1, on_accepted=Mock(),
                                      on_failure=Mock(), binding_status=Mock())
                selected_scope = replace(scope(), api_origin=jobs_api_peer.origin)
                endpoint = DeliveryEndpoint(selected_scope.api_origin, selected_scope.provider_ref,
                                            report.parent / "unused-relay.sock")
                delivery_transport = HttpTransport(trust=jobs_api_peer.trust, exchange_seconds=12, retirement_seconds=0.3)
                delivery_api = api(delivery_transport, jobs_api_peer.origin)
                publisher = DeliveryWorker(inbox, endpoint, delivery_api, retry_initial_seconds=0.01,
                                           retry_max_seconds=0.04, on_notice=notice)
                original_complete = delivery_api.complete_attempt

                async def observe_completion_receipt(body, **settings):
                    receipt = await original_complete(body, **settings)
                    completion_receipts.append(receipt)
                    return receipt

                patches.enter_context(patch.object(delivery_api, "complete_attempt", new=observe_completion_receipt))
                publisher_task = asyncio.create_task(publisher.run())
                local_delivery = DirectDurableHandoff(inbox, endpoint, publisher)
                idle_descriptors = _descriptors()

                async def run_job(name, route):
                    job_ref = "job:b3-" + name
                    base = record("b3:" + name)
                    pending = StartPending(selected_scope, base.analysis_kind_ref, replace(base.input, job_ref=job_ref),
                                           retained_start(job_ref=job_ref, provider_attempt_key="b3:" + name))
                    session = ScientificSession(job_ref, prose_peer.origin + route, prose_peer.trust, scientific_worker)
                    control_transport = JobsControlTransport(job_ref)
                    controller = AttemptController.live(
                        api(control_transport, selected_scope.api_origin), claims, session, scope=selected_scope,
                        stopping=provider_stopping, start_policy=LiveStartPolicy(0.01, Mock(), Mock()),
                        preverified_input=verify_job_input(response(job_ref=job_ref), feed=pending.input),
                        delivery=local_delivery, on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
                        delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                        poll_seconds=1, task_settle_seconds=0.3,
                    )
                    jobs.append((name, session, control_transport, controller))
                    controller_task = asyncio.create_task(controller.run(pending))
                    await asyncio.wait_for(asyncio.shield(controller_task), 8)
                    return session, control_transport, controller

                async def wait_for_publication():
                    wanted = {item.execution_attempt_ref for item in local_delivery.items}
                    deadline = time.monotonic() + 6
                    while not wanted <= published and time.monotonic() < deadline:
                        await asyncio.sleep(0.005)
                    if not wanted <= published:
                        raise AssertionError("Original delivery worker did not drain the exact queued outcomes")

                async def release_receipt_during_fault():
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if acknowledgement_phase == "request_received":
                            seen = sum(request.split(b" ")[1] == b"/hang" for request in prose_peer.requests)
                        else:
                            events = []
                            if marker.exists():
                                events = [json.loads(line) for line in marker.read_text().splitlines()]
                            seen = sum(event["kind"] == "cleanup_engaged" for event in events)
                        if seen >= acknowledgement_cycle:
                            result["acknowledgement_released_during_cycle"] = seen
                            result["live_claims_at_acknowledgement_release"] = len(claims.live_owners())
                            result["original_api_request_owned_at_acknowledgement_release"] = delivery_transport.pending_owner is publisher
                            result["open_prose_connections_at_acknowledgement_release"] = sum(not task.done() for task in prose_peer.handlers)
                            release_api_receipt.set()
                            return
                        await asyncio.sleep(0.005)
                    raise AssertionError("The requested HTTP failure boundary never occurred")

                try:
                    await run_job("original-result", "/ready")
                    await asyncio.wait_for(completed_result_received.wait(), 8)
                    await _settled_peer_handlers(prose_peer)
                    original_result = local_delivery.items[0]
                    pending_delivery_descriptors = _descriptors()
                    result.update(provider_pid=os.getpid(), maximum_attempts=maximum_attempts,
                                  repetitions=repetitions, scientific_worker_before=scientific_worker.initial,
                                  idle_descriptors=idle_descriptors,
                                  pending_delivery_descriptors=pending_delivery_descriptors,
                                  original_result_acceptance=original_result.acceptance())
                    release_task = asyncio.create_task(release_receipt_during_fault())
                    for number in range(1, repetitions + 1):
                        session, control_transport, controller = await run_job("timeout-" + str(number), "/hang")
                        http_transport = session.http_endpoints[0]._transport
                        process = http_transport.last_process_result
                        first_stop = first_http_stops[http_transport]
                        if release_api_receipt.is_set():
                            await wait_for_publication()
                            await _settled_peer_handlers(jobs_api_peer)
                        open_prose_connections = await _settled_peer_handlers(prose_peer)
                        events = [json.loads(line) for line in marker.read_text().splitlines()]
                        process_events = {event["kind"] for event in events if event["pid"] == process.pid}
                        diagnostic = failure_evidence(session.failure, secrets=("PRIVATE_B3_FIXTURE_KEY",))
                        snapshot = {
                            "number": number, "provider_pid": os.getpid(),
                            "http_child_pid": process.pid, "http_child_returncode": process.returncode,
                            "http_child_alive": _process_alive(process.pid), "signals_sent": list(process.signals_sent),
                            "fault_events": sorted(process_events), "open_prose_connections": open_prose_connections,
                            "live_claims": len(claims.live_owners()), "controller_shutdown_ready": controller.shutdown_ready,
                            "descriptors": _descriptors(), "receipt_released": release_api_receipt.is_set(),
                            "delivery_worker_running": not publisher_task.done(),
                            "delivery_original_request_owned": delivery_transport.pending_owner is publisher,
                            "original_result_pending": queue.next_pending(*original_result.key[:2]) == original_result,
                            "scientific_worker": await scientific_worker.call("snapshot"),
                            "first_cause_is_timeout": isinstance(first_stop, TimeoutError),
                            "original_first_cause_retained": http_transport.last_failure.primary is first_stop,
                            "original_interpreter_failure_retained": session.interpreter.last_failure is session.failure,
                            "timeout_in_failure_graph": "TimeoutError" in _failure_types(session.failure),
                            "timeout_in_diagnostic": '"exception_type": "TimeoutError"' in json.dumps(diagnostic),
                        }
                        cycles.append(snapshot)
                    await release_task
                    await run_job("next-healthy", "/ready")
                    await wait_for_publication()
                    open_prose_connections = await _settled_peer_handlers(prose_peer)
                    open_jobs_api_connections = await _settled_peer_handlers(jobs_api_peer)
                    for request, body in zip(jobs_api_peer.requests, jobs_api_peer.request_bodies, strict=True):
                        verify_signed_request(request, body, origin=jobs_api_peer.origin, key=delivery_api.identity.private_key)
                    result.update(
                        scientific_worker_after=await scientific_worker.call("snapshot"),
                        original_result_bytes_sent_once=jobs_api_peer.request_bodies.count(original_result.body) == 1,
                        original_result_receipt_reference=completion_receipts[0].execution_attempt_ref,
                        original_result_receipt_result="analysis_result:sha256:" + sha256(original_result.body).hexdigest(),
                        original_result_receipt_verified=completion_receipts[0].analysis_result_ref,
                        delivery_request_bodies_match=[item.body for item in local_delivery.items] == jobs_api_peer.request_bodies,
                        published_attempts=sorted(published), notices=notices,
                        queue_inventory=queue.inventory(selected_scope.api_origin, selected_scope.provider_ref),
                        final_live_claims=len(claims.live_owners()), final_descriptors=_descriptors(),
                        final_open_prose_connections=open_prose_connections,
                        final_open_jobs_api_connections=open_jobs_api_connections,
                        jobs=[{"name": name, "session_prepare_calls": session.prepare_calls,
                               "scientific_prepare_calls": session.scientific_prepare_calls,
                               "scientific_execute_calls": session.execute_calls,
                               "starts": sum(request.url.path.endswith("/start") for request, _ in control.calls),
                               "shutdown_ready": controller.shutdown_ready}
                              for name, session, control, controller in jobs],
                        prose_request_paths=[request.split(b" ")[1].decode() for request in prose_peer.requests],
                    )
                except Exception as failure:
                    result["scenario_failure"] = {"type": type(failure).__name__, "traceback": traceback.format_exc()}
                # Capture before the outer test stops the fixture's process group.
                # That emergency cleanup must never make a recovery assertion pass.
                temporary = report.with_suffix(".pending")
                temporary.write_text(json.dumps(result, sort_keys=True))
                temporary.replace(report)
                await asyncio.Event().wait()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if sys.argv[1] == "http-worker":
        from tests.fakes.chat_process_fault import http_worker

        http_worker(Path(sys.argv[2]))
    elif sys.argv[1] == "scenario":
        asyncio.run(scenario(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])))
    else:
        raise SystemExit("Unknown B3 repetition fixture role")
