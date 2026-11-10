"""Stop a real SDK Attempt while its prose-interpreter HTTP child is starting or waiting.

The fixture uses the controller's active-Attempt stop contract. A provider's
operator signal may stop admission before it invokes that contract. The HTTPS
peer, API replies and scientific adapter are test-owned; the controller,
Interpreter, HTTP subprocess, socket and process-exit observation are real.
The outer test reads the verdict before it tears down this disposable process.
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import signal
import sys
from unittest.mock import Mock, patch


@dataclass(frozen=True)
class ShutdownSchedule:
    stage: str
    ignore_stop_signals: bool
    stop_requests: int
    cancelled_join_observers: int
    scheduling_turns: int


def seeded_schedule(seed):
    randomizer = random.Random(seed)
    stage = randomizer.choice(("startup", "https_wait", "late_startup", "late_exit_observation"))
    return ShutdownSchedule(
        stage=stage,
        ignore_stop_signals=stage in {"https_wait", "late_exit_observation"} and randomizer.choice((False, True)),
        stop_requests=randomizer.randint(1, 3),
        cancelled_join_observers=randomizer.randint(0, 2),
        scheduling_turns=randomizer.randint(0, 3),
    )


def failure_types(error):
    from provider_core.failure_projection import owned_relationships

    seen, names = set(), set()

    def visit(current):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        names.add(type(current).__name__)
        visit(current.__cause__)
        visit(current.__context__)
        for _, child in owned_relationships(current):
            visit(child)
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                visit(child)

    visit(error)
    return sorted(names)


def process_is_alive(pid):
    try:
        os.kill(pid, 0)
    # silent-defaults: allow-exception-outcome -- ESRCH is a definitive negative liveness observation for the selected child process.
    except ProcessLookupError:
        return False
    return True


def selected_start(label):
    from provider_core.attempt_record import StartPending
    from tests.fakes.attempt_store import record, scope
    from tests.fakes.attempts import retained_start

    job = "job:" + label
    base = record("shutdown:" + label)
    return StartPending(
        scope(), base.analysis_kind_ref, replace(base.input, job_ref=job),
        retained_start(job_ref=job, provider_attempt_key="shutdown:" + label),
    )


def uncooperative_http_child(marker):
    """Keep the real HTTP exchange running after both parent stop signals."""
    from provider_core.chat_worker import _Cancellation, main
    from tests.fakes.chat_process_fault import _event

    def ignore_stop_request(_cancellation, _number, _frame):
        _event(marker, "stop_request_ignored")

    def ignore_termination(_number, _frame):
        _event(marker, "term_ignored")

    signal.signal(signal.SIGTERM, ignore_termination)
    with patch.object(_Cancellation, "request", ignore_stop_request):
        main()


async def shutdown_scenario(schedule, seed, directory, facts):
    import httpx
    import provider_core.chat_process as chat_process
    from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
    from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
    from provider_core.interpreter import Interpreter
    from provider_core.job_input import verify_job_input
    from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull, OwnershipRefused
    from provider_core.transport_errors import ExchangeFailed
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.attempt_store import scope
    from tests.fakes.attempts import observation, progress_response, start_response
    from tests.fakes.checkpoint_execution import RecordingDelivery
    from tests.fakes.job_input import response
    from tests.fakes.tls_peer import TlsPeer
    from tests.fakes.conversation import call
    from tests.integration.chat.adversarial.test_model_reply_permission import InterpretedSession, ReplyRun

    loop = asyncio.get_running_loop()
    cleanup_seconds = 0.6
    trace = facts["trace"]

    def note(event, **facts):
        trace.append({"event": event, **facts})

    class JobsApiReplies(Transport):
        def __init__(self, pending):
            super().__init__()
            self.job = pending.input.job_ref
            self.attempt = "execution_attempt:sha256:" + sha256(self.job.encode()).hexdigest()

        async def exchange(self, request, *, maximum_bytes, owner=None):
            self.calls.append((request, maximum_bytes))
            if request.url.path.endswith("/start"):
                return reply(start_response(job_ref=self.job, execution_attempt_ref=self.attempt))
            if request.url.path.endswith("/progress"):
                progress = json.loads(request.content)
                return reply(progress_response(execution_attempt_ref=self.attempt, phase=progress["phase"],
                                               condition_code=progress["condition_code"]))
            if request.method == "GET":
                return reply(observation(job_ref=self.job, execution_attempt_ref=self.attempt))
            raise AssertionError("The shutdown fixture received an unexpected Jobs API request")

    class ProseInterpretationSession:
        def __init__(self, peer):
            self.prepare_calls = self.execute_calls = self.tool_dispatches = self.retire_calls = 0
            self.endpoint = None
            self.retired = False
            factory = ChatEndpointFactory(
                spec=ChatEndpointSpec(
                    configuration_id="prose-interpreter-shutdown-test", model="local-test-peer",
                    url=peer.origin + "/hang", api_key="PRIVATE_FIXTURE_KEY", reasoning_effort=None,
                ),
                trust=peer.trust, retirement_seconds=cleanup_seconds,
                request_bytes=32768, response_bytes=8192,
            )

            def observed_factory(**settings):
                if self.endpoint is not None:
                    raise AssertionError("Stopping interpretation created a second HTTP endpoint")
                self.endpoint = factory(**settings)
                return self.endpoint

            self.interpreter = Interpreter(
                (observed_factory,), timeout_seconds=30, context_bytes=16384, secrets=("PRIVATE_FIXTURE_KEY",),
            )

        async def prepare(self, acknowledged, instructions, *, report_condition):
            self.prepare_calls += 1
            if acknowledged.start.input != instructions.identity:
                raise AssertionError("Stopping preparation replaced the selected Job input")

            async def dispatch(name, arguments, *, not_after):
                report_condition.require_execution()
                self.tool_dispatches += 1
                raise AssertionError("The unanswered HTTPS request unexpectedly dispatched scientific work")

            tools = [{"type": "function", "function": {
                "name": "prepare", "parameters": {"type": "object", "properties": {}},
            }}]
            return (await self.interpreter.run([], tools, dispatch)).value

        async def execute(self, prepared, *, report_condition):
            self.execute_calls += 1
            raise AssertionError("The stopped Attempt unexpectedly executed science")

        async def retire(self):
            self.retire_calls += 1
            await self.interpreter.retire()
            if not self.interpreter.retirement_confirmed:
                raise AssertionError("The session declared cleanup complete while its HTTP child was still owned")
            self.retired = True
            note("session_retirement_confirmed")

    children = []
    independent_children = []
    exit_observer_tasks = []
    child_exit_times = []
    signals = []
    helper_created = asyncio.Event()
    allow_creation_return = asyncio.Event()
    actual_child_exit = asyncio.Event()
    allow_exit_observation = asyncio.Event()
    original_launch = asyncio.create_subprocess_exec
    startup_held = schedule.stage in {"startup", "late_startup"}
    concurrent = schedule.stage == "concurrent_late_exit_observation"
    exit_observation_held = schedule.stage in {"late_exit_observation", "concurrent_late_exit_observation"}
    if not startup_held:
        allow_creation_return.set()
    if not exit_observation_held:
        allow_exit_observation.set()

    async def observed_launch(*args, **kwargs):
        child = await original_launch(*args, **kwargs)
        if concurrent and children:
            # This second real supervised child is not the original's delayed
            # exit observer. Observe and reap it normally.
            independent_children.append(child)
            note("independent_operating_system_child_created", pid=child.pid)
            return child
        children.append(child)
        original_wait = child.wait
        original_signal = child.send_signal

        async def observed_wait():
            exit_observer_tasks.append(asyncio.current_task())
            returncode = await original_wait()
            child_exit_times.append(loop.time())
            note("operating_system_reported_child_exit", pid=child.pid, returncode=returncode)
            actual_child_exit.set()
            await allow_exit_observation.wait()
            note("original_exit_observation_returned", pid=child.pid)
            return returncode

        def observed_signal(number):
            signals.append((int(number), loop.time()))
            note("sdk_signalled_http_child", pid=child.pid, signal=int(number))
            return original_signal(number)

        child.wait = observed_wait
        child.send_signal = observed_signal
        note("operating_system_created_http_child", pid=child.pid)
        helper_created.set()
        await allow_creation_return.wait()
        note("original_creation_await_returned", pid=child.pid)
        return child

    marker = directory / f"http-cleanup-{seed}.jsonl"
    pending = selected_start(f"{schedule.stage}-{seed}")
    # silent-defaults: allow-contract -- The concurrent scenario needs capacity for the original held Attempt and one independent successor.
    claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_attempts=2 if concurrent else 1))
    async with TlsPeer("headers-stall") as peer, AsyncExitStack() as patches:
        session = ProseInterpretationSession(peer)
        jobs_api_replies = JobsApiReplies(pending)
        controller = AttemptController.live(
            api(jobs_api_replies, scope().api_origin), claims, session,
            scope=scope(), stopping=asyncio.Event(), start_policy=LiveStartPolicy(1, Mock(), Mock()),
            preverified_input=verify_job_input(response(job_ref=pending.input.job_ref), feed=pending.input),
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=0.01, task_settle_seconds=0.05,
        )
        patches.enter_context(patch.object(chat_process.asyncio, "create_subprocess_exec", side_effect=observed_launch))
        if schedule.ignore_stop_signals:
            fault_entrypoint = Path(__file__).resolve()
            command = (sys.executable, "-I", "-B", str(fault_entrypoint), "http-child", str(marker))
            patches.enter_context(patch.object(chat_process, "_worker_command", return_value=command))
        descriptors_before_launch = len(os.listdir("/proc/self/fd"))
        running = asyncio.create_task(controller.run(pending))
        try:
            if startup_held:
                await asyncio.wait_for(helper_created.wait(), 4)
            else:
                await asyncio.wait_for(peer.received.wait(), 4)
                note("test_https_server_received_interpretation_request")
            http_transport = session.endpoint._transport
            original_exchange = http_transport._active
            original_owner = http_transport.pending_owner
            original_deadline = original_exchange.deadline
            original_driver = controller._driver
            trust_directory = Path(original_exchange.trust_directory.name)

            for request_number in range(schedule.stop_requests):
                controller.request_stop()
                note("provider_requested_active_attempt_stop", request_number=request_number + 1)
                for _ in range(schedule.scheduling_turns):
                    await asyncio.sleep(0)

            cancelled_observers = 0
            for observer_number in range(schedule.cancelled_join_observers):
                observer = asyncio.create_task(controller.join())
                for _ in range(schedule.scheduling_turns):
                    await asyncio.sleep(0)
                if not observer.done():
                    observer.cancel()
                result, = await asyncio.gather(observer, return_exceptions=True)
                if isinstance(result, asyncio.CancelledError):
                    cancelled_observers += 1
                note("join_observer_finished", observer_number=observer_number + 1,
                     cancelled=isinstance(result, asyncio.CancelledError))

            await asyncio.wait_for(original_exchange.stop.wait(), 2)
            original_stop_time = original_exchange.stopped_at
            note("sdk_stopped_original_http_exchange")
            if exit_observation_held:
                await asyncio.wait_for(actual_child_exit.wait(), 2)
            if schedule.stage in {"late_startup", "late_exit_observation", "concurrent_late_exit_observation"}:
                allowance_elapsed = asyncio.Event()
                loop.call_at(original_stop_time + cleanup_seconds + 0.05, allowance_elapsed.set)
                await asyncio.wait_for(allowance_elapsed.wait(), 2)
                note("original_http_cleanup_allowance_elapsed")

            if concurrent:
                # A's actual child has already exited, but its exact original
                # wait() task is deliberately withholding that exit observation.
                # With two capacity slots, unrelated B must still make progress
                # through its own actual subprocess and delivery handoff.
                if not actual_child_exit.is_set() or allow_exit_observation.is_set():
                    raise AssertionError("No held original child-exit observation")
                completion = json.dumps({
                    "choices": [{"message": call("completed", "prepare", {}),
                                 "finish_reason": "tool_calls"}]
                }).encode()
                async with TlsPeer("success", response_body=completion) as independent_peer:
                    independent = ReplyRun(
                        independent_peer, claims, f"job:independent-{seed}",
                        InterpretedSession,
                    )
                    independent.session.science.release.set()
                    independent_result = await asyncio.wait_for(
                        independent.controller.run(independent.pending), 6)
                    original_waiter = exit_observer_tasks[0]
                    facts["independent"] = {
                        "result_type": type(independent_result).__name__,
                        "start_count": independent.start_count,
                        "preparations": independent.session.science.preparations,
                        "executions": independent.session.science.executions,
                        "retirements": independent.session.science.retirements,
                        "interpreter_retired": independent.session.interpreter.retirement_confirmed,
                        "local_terminal_acks": len(independent.delivery.items),
                        "child_pids": [child.pid for child in independent_children],
                        "child_returncodes": [child.returncode for child in independent_children],
                        "child_stdin_closed": [child.stdin.is_closing() for child in independent_children],
                        "http_requests": len(independent_peer.requests),
                        "original_child_exited": not process_is_alive(children[0].pid),
                        "original_waiter_unfinished": not original_waiter.done(),
                        "original_observation_gate_closed": not allow_exit_observation.is_set(),
                        "original_transport_held": not http_transport.retirement_confirmed,
                        "original_job_owned": claims.owns_job(pending.scope, pending.input.job_ref),
                        "independent_job_released": not claims.owns_job(
                            independent.pending.scope, independent.pending.input.job_ref),
                        "live_claim_count": len(claims.live_owners()),
                    }
                    note("independent_real_child_and_science_settled",
                         pid=independent_children[0].pid,
                         original_waiter_unfinished=not original_waiter.done())

            if startup_held or exit_observation_held:
                facts["held"] = {
                    "shutdown_ready": controller.shutdown_ready,
                    "session_retired": session.retired,
                    "http_retirement_confirmed": http_transport.retirement_confirmed,
                    "original_owner_retained": http_transport.pending_owner is original_owner,
                    "original_exchange_retained": http_transport._active is original_exchange,
                    "claim_count": len(claims.live_owners()),
                    "child_alive": process_is_alive(children[0].pid),
                    "actual_child_exit_observed": actual_child_exit.is_set(),
                    "trust_directory_exists": trust_directory.is_dir(),
                }
                if concurrent:
                    # There is room for B, but never another owner of A's
                    # still-held original START key and Job.
                    try:
                        claims.claim_live(pending, lifecycle_owner=object(), maximum_terminal_bytes=16384)
                    except OwnershipRefused:
                        facts["held"]["original_job_refused"] = True
                    else:
                        facts["held"]["original_job_refused"] = False
                else:
                    replacement = selected_start(f"replacement-{schedule.stage}-{seed}")
                    try:
                        claims.claim_live(replacement, lifecycle_owner=object(), maximum_terminal_bytes=16384)
                    except LiveCapacityFull:
                        facts["held"]["replacement_job_refused"] = True
                    else:
                        facts["held"]["replacement_job_refused"] = False
                try:
                    await asyncio.wait_for(http_transport.exchange_decoded(
                        httpx.Request("POST", peer.origin + "/replacement", content=b"{}"),
                        maximum_bytes=8192, error_maximum_bytes=8192,
                        owner=object(), not_after=loop.time() + 30,
                    ), 1)
                except ExchangeFailed:
                    facts["held"]["replacement_http_request_refused"] = True
                else:
                    facts["held"]["replacement_http_request_refused"] = False

            if schedule.stage in {"late_startup", "late_exit_observation", "concurrent_late_exit_observation"}:
                probe_started = loop.time()
                probe = asyncio.create_task(session.endpoint.retire())
                await asyncio.wait({probe}, timeout=cleanup_seconds / 2)
                facts["held"]["repeated_retirement_finished_without_new_allowance"] = probe.done()
                facts["held"]["repeated_retirement_seconds"] = loop.time() - probe_started
                if probe.done():
                    probe_result, = await asyncio.gather(probe, return_exceptions=True)
                    facts["held"]["retirement_failure_types"] = failure_types(probe_result)
                else:
                    probe.cancel()
                    await asyncio.gather(probe, return_exceptions=True)

            allow_creation_return.set()
            allow_exit_observation.set()
            release_time = loop.time()
            note("test_released_original_os_observations")
            result, = await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 3)
            note("original_controller_run_finished", result_type=type(result).__name__)
            settlement_failures = []
            settle_deadline = loop.time() + 2
            while not controller.shutdown_ready and loop.time() < settle_deadline:
                try:
                    await controller.settle()
                except ControllerFailed as failure:
                    settlement_failures.append(failure)
                if not controller.shutdown_ready:
                    await asyncio.sleep(0.01)
            if peer.requests:
                await asyncio.wait_for(peer.closed.wait(), 1)
                await asyncio.wait_for(asyncio.gather(*peer.handlers), 1)
            await asyncio.sleep(0)
            faults = []
            if marker.exists():
                faults = [json.loads(line) for line in marker.read_text().splitlines()]
            facts.update({
                "controller_failure_types": failure_types(result),
                "settlement_failure_types": sorted({name for failure in settlement_failures
                                                    for name in failure_types(failure)}),
                "shutdown_ready": controller.shutdown_ready,
                "work_settled": controller.work_settled,
                "session_retired": session.retired,
                "http_retirement_confirmed": http_transport.retirement_confirmed,
                "original_driver_retained": controller._driver is original_driver,
                "request_deadline_unchanged": original_exchange.deadline == original_deadline,
                "stop_time_unchanged": original_exchange.stopped_at == original_stop_time,
                "prepare_calls": session.prepare_calls, "execute_calls": session.execute_calls,
                "tool_dispatches": session.tool_dispatches,
                "start_count": sum(request.url.path.endswith("/start") for request, _ in jobs_api_replies.calls),
                "http_request_count": len(peer.requests),
                "child_count": len(children), "child_pids": [child.pid for child in children],
                "child_returncodes": [child.returncode for child in children],
                "child_still_alive": [process_is_alive(child.pid) for child in children],
                "child_stdin_closed": [child.stdin.is_closing() for child in children],
                "exit_observer_count": len(exit_observer_tasks),
                "exit_observer_cancelled": any(task.cancelled() for task in exit_observer_tasks),
                "cancelled_join_observers": cancelled_observers,
                "socket_closed_by_child": peer.closed.is_set(),
                "unfinished_peer_connections": sum(not task.done() for task in peer.handlers),
                "descriptors_before_launch": descriptors_before_launch,
                "descriptors_after_cleanup": len(os.listdir("/proc/self/fd")),
                "trust_directory_exists": trust_directory.exists(),
                "claim_count_after_local_stop": len(claims.live_owners()),
                "cleanup_seconds": cleanup_seconds,
                "child_exit_seconds_after_stop": [at - original_stop_time for at in child_exit_times],
                "observations_released_seconds_after_stop": release_time - original_stop_time,
                "shutdown_seconds_after_releasing_observations": loop.time() - release_time,
                "signal_delays": [(number, at - original_stop_time) for number, at in signals],
                "http_fault_events": [event["kind"] for event in faults],
            })
        finally:
            allow_creation_return.set()
            allow_exit_observation.set()
            # A failing behavioral verdict must not be made green by this
            # fixture's teardown. The outer process group contains any survivor.
            if running.done():
                await asyncio.gather(running, return_exceptions=True)
    return facts


async def main(report, mode, seed):
    completed = []
    current = None
    try:
        case_seeds = (seed,)
        if mode == "seeded":
            case_seeds = range(seed)
        for case_seed in case_seeds:
            if mode == "seeded":
                schedule = seeded_schedule(case_seed)
            else:
                stage = mode
                if mode == "uncooperative_https":
                    stage = "https_wait"
                schedule = ShutdownSchedule(stage, mode == "uncooperative_https", 1, 0, 0)
            current = {"seed": case_seed, "schedule": asdict(schedule), "trace": []}
            await shutdown_scenario(schedule, case_seed, report.parent, current)
            completed.append(current)
        verdict = {"scenarios": completed}
    except BaseException as failure:
        import traceback
        verdict = {"fixture_failure": type(failure).__name__, "traceback": traceback.format_exc(),
                   "current_scenario": current, "completed_scenarios": completed}
    temporary = report.with_suffix(".pending")
    temporary.write_text(json.dumps(verdict, sort_keys=True))
    temporary.replace(report)
    await asyncio.Event().wait()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if sys.argv[1] == "http-child":
        uncooperative_http_child(Path(sys.argv[2]))
    else:
        asyncio.run(main(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
