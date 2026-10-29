"""Draft installed-SDK subprocess witness; run through a contained SDK lane.

Requires installed provider_core and the existing tests.fakes package. This
script deliberately does not set or bypass the SDK's container policy guard.
"""

import asyncio
from concurrent.futures import Future
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def failure_graph(error):
    """Follow the same original relationships as the protected diagnostic renderer."""
    from provider_core.failure_projection import owned_relationships

    seen = {}

    def visit(current):
        if current is None or id(current) in seen:
            return
        seen[id(current)] = current
        visit(current.__cause__)
        visit(current.__context__)
        for _, child in owned_relationships(current):
            visit(child)
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                visit(child)

    visit(error)
    return tuple(seen.values())


async def child(owner_kind, failure_kind):
    from unittest.mock import Mock
    from provider_core.async_runner_session import await_original
    from provider_core.controller import ControllerFailed
    from provider_core.execution import ExecutionFailed
    from provider_core.ownership import AttemptClaims
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.attempt_store import record
    from tests.fakes.attempts import observation, progress_response, start_response
    from tests.fakes.checkpoint_execution import RecordingDelivery
    from tests.fakes.execution import Session, LiveAttemptProbe, live_attempt, live_controller, live_executor
    from tests.fakes.job_input import response

    if owner_kind == "controller":
        probe = LiveAttemptProbe()
        acknowledged = None
        prefix = [reply(start_response())]
    else:
        probe, acknowledged, claim = await live_attempt()
        prefix = []
    cancelling = failure_kind.startswith("cancel")
    primary_kind = failure_kind.split("-cleanup-")[0]
    cleanup_kind = None
    if "-cleanup-" in failure_kind:
        cleanup_kind = failure_kind.split("-cleanup-")[1]
    entered = asyncio.Event()
    captured_cancellation = []
    original = Future()
    fatal = None
    if not cancelling:
        fatal = {
            "system-exit": SystemExit,
            "keyboard-interrupt": KeyboardInterrupt,
            "ordinary": OSError,
        }[primary_kind]("PRIVATE authored native failure")
    if fatal is not None:
        original.set_exception(fatal)
    session = Session(probe)
    resources = []
    cleanup = None
    if cleanup_kind is not None:
        cleanup = {"system-exit": SystemExit, "keyboard-interrupt": KeyboardInterrupt}[cleanup_kind]("PRIVATE cleanup fatal")
        session.retirement_failure = cleanup

    async def prepare(_acknowledged, _instructions, *, report_condition):
        session.events.append(("prepare",))
        resources.append(await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"))
        entered.set()
        try:
            return await await_original(original, deadline=None)
        except asyncio.CancelledError as caught:
            captured_cancellation.append(caught)
            raise
    session.prepare = prepare
    scripted_retire = session.retire
    async def retire():
        for resource in resources:
            if resource.returncode is None:
                resource.terminate()
            await asyncio.wait_for(resource.wait(), 1)
        await scripted_retire()
    session.retire = retire

    class Delivery(RecordingDelivery):
        def __init__(self):
            super().__init__()
            self.retire_calls = 0

        async def retire(self):
            self.retire_calls += 1
            await asyncio.sleep(0)
            await super().retire()

    delivery = Delivery()
    transport = Transport(*prefix, reply(observation()), reply(response()), reply(progress_response()))
    client = api(transport, probe.scope.api_origin)
    settings = dict(delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                    delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1, task_settle_seconds=1)
    if owner_kind == "controller":
        owner = live_controller(client, probe, AttemptClaims.live(), session, **settings)
        task = asyncio.create_task(owner.run(record()))
    else:
        owner = live_executor(client, probe, claim, session, **settings)
        task = asyncio.create_task(owner.run(acknowledged))
    await entered.wait()
    if cancelling:
        if owner_kind == "controller":
            # Controller.run cancellation intentionally leaves its driver alive.
            owner._driver.cancel()
        else:
            task.cancel()
    try:
        await task
    except asyncio.CancelledError as caught:
        assert cancelling
        assert captured_cancellation
        assert owner.last_failure.cause is caught
        executor = owner.operation if owner_kind == "controller" else owner
        assert executor.last_failure.operation_interruption is captured_cancellation[0]
    except (ControllerFailed, ExecutionFailed, BaseExceptionGroup) as caught:
        assert not cancelling
        assert owner.last_failure is not None
        failure = caught
        while isinstance(failure, (ControllerFailed, ExecutionFailed)):
            failure = failure.cause
        if cleanup is not None and primary_kind == "ordinary":
            assert isinstance(failure, BaseExceptionGroup)
            assert cleanup in failure_graph(failure)
            primary_record = owner.operation.last_failure if owner_kind == "controller" else owner.last_failure
            assert primary_record.cause is fatal
        elif primary_kind == "ordinary":
            assert failure is fatal
        else:
            assert isinstance(failure, BaseExceptionGroup)
            assert fatal in failure_graph(failure)
        assert original.exception() is fatal
    else:
        raise AssertionError("scripted failure did not reach its retained owner")
    assert [event[0] for event in session.events] == ["prepare", "retire"]
    assert delivery.retire_calls >= 1
    assert len(resources) == 1 and resources[0].returncode is not None, "the original child must actually be reaped"
    assert not delivery.items, "an authored Python process exit must not synthesize terminal FAIL"
    assert len(transport.calls) == 3 + len(prefix), "no science replay or later API publication"
    assert not original.cancelled()
    if cleanup is not None:
        failure_record = owner.last_failure
        if owner_kind == "controller":
            failure_record = owner.operation.last_failure
        assert any(isinstance(cause, BaseExceptionGroup) and cause.exceptions == (cleanup,)
                   for cause in failure_record.cleanup_failures)
    if cancelling:
        assert not original.done(), "observer cancellation must leave original native work intact"
        original.set_result(None)
    print(owner_kind + "/" + failure_kind + ": retained original failure, independent cleanup, no terminal submission")


async def owned_port_child(stage, failure_kind):
    from unittest.mock import Mock
    from provider_core.async_runner_session import await_original
    from provider_core.attempt_record import TerminalOperation
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.attempts import observation, progress_response
    from tests.fakes.checkpoint_execution import RecordingDelivery
    from tests.fakes.execution import Session, live_attempt, live_executor
    from tests.fakes.job_input import response

    probe, acknowledged, claim = await live_attempt()
    original = Future()
    fatal = {"system-exit": SystemExit, "keyboard-interrupt": KeyboardInterrupt}[failure_kind]("PRIVATE authored owned-port failure")
    original.set_exception(fatal)
    session = Session(probe)
    swallowed = []
    if stage == "retirement":
        async def retire():
            session.events.append(("retire",))
            await asyncio.sleep(0)
            await await_original(original, deadline=None)
        session.retire = retire
    if stage == "condition":
        async def prepare(_acknowledged, _instructions, *, report_condition):
            session.events.append(("prepare",))
            try:
                await report_condition("warming_up")
            except BaseExceptionGroup as caught:
                swallowed.append(caught)
            return session.handle
        session.prepare = prepare

    class Delivery(RecordingDelivery):
        def __init__(self):
            super().__init__()
            self.retire_calls = 0
            self.submit_calls = 0

        async def submit(self, item):
            self.submit_calls += 1
            if stage == "handoff":
                return await await_original(original, deadline=None)
            return await super().submit(item)

        async def retire(self):
            self.retire_calls += 1
            await asyncio.sleep(0)
            await super().retire()

    delivery = Delivery()
    transport = Transport(reply(observation()), reply(response()), reply(progress_response()),
                          reply(observation()), reply(progress_response(phase="running")),
                          reply(observation()), reply(observation()))
    client = api(transport, probe.scope.api_origin)
    if stage == "condition":
        ordinary_progress = client.progress_attempt
        async def progress(reference, *, phase, condition_code, owner):
            if condition_code is not None:
                return await await_original(original, deadline=None)
            return await ordinary_progress(reference, phase=phase, condition_code=condition_code, owner=owner)
        client.progress_attempt = progress
    owner = live_executor(client, probe, claim, session, delivery=delivery,
                          on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                          delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                          poll_seconds=1, task_settle_seconds=1)
    try:
        await asyncio.create_task(owner.run(acknowledged))
    except BaseExceptionGroup as caught:
        assert fatal in failure_graph(caught)
    else:
        raise AssertionError("owned port fatal was replaced or lost")
    assert owner.last_failure is not None
    assert original.exception() is fatal
    assert delivery.retire_calls >= 1
    assert [event[0] for event in session.events].count("retire") == 1
    if stage == "condition":
        assert swallowed and owner.last_failure.cause is swallowed[0]
        assert [event[0] for event in session.events] == ["prepare", "retire"]
        assert delivery.submit_calls == 0 and not delivery.items
        assert len(transport.calls) == 3
    else:
        assert [event[0] for event in session.events] == ["prepare", "execute", "retire"]
        assert len(transport.calls) == 7
        assert delivery.submit_calls == 1
        if stage == "retirement":
            assert len(delivery.items) == 1 and delivery.items[0].operation is TerminalOperation.COMPLETE
            assert owner._outcome is None and not owner.retirement_confirmed
        else:
            assert not delivery.items
            assert owner._outcome is session.outcome and owner.retirement_confirmed
    print(stage + "/" + failure_kind + ": exact fatal retained, independent cleanup and selected-report duties preserved")



async def graph_child(stage, variant):
    """Retain independent cleanup through the actual protected diagnostic surface."""
    import errno
    import json
    from unittest.mock import Mock
    from provider_core.diagnostics import failure_evidence
    from provider_core.execution import ExecutionFailed
    from provider_core.ownership import AttemptClaims
    from tests.fakes.api import Transport, api, reply
    from tests.fakes.attempt_store import record
    from tests.fakes.attempts import observation, progress_response
    from tests.fakes.checkpoint_execution import RecordingDelivery
    from tests.fakes.execution import Session, live_attempt, live_controller, live_executor
    from tests.fakes.job_input import response

    probe, acknowledged, claim = await live_attempt()
    session = Session(probe)
    primary = BaseExceptionGroup("authored primary", (KeyboardInterrupt("PRIVATE primary"),))
    previous = LookupError("PRIVATE original cause")
    if variant == "chained":
        primary.__cause__ = previous
    retirement = OSError(errno.EIO, "PRIVATE retirement")
    delivery_failure = BaseExceptionGroup("authored delivery", (SystemExit("PRIVATE delivery"),))
    if variant == "multiple" or stage == "graph-settle":
        primary = ValueError("PRIVATE primary")
        retirement = BaseExceptionGroup("authored retirement", (KeyboardInterrupt("PRIVATE retirement"),))
        retirement.__cause__ = previous
    if stage == "graph-admission":
        primary = SystemExit("PRIVATE admission")
    if stage == "graph-controller-delivery":
        delivery_failure = KeyboardInterrupt("PRIVATE delivery")
    if variant == "cancel":
        primary = asyncio.CancelledError("PRIVATE cancellation")
        primary.__cause__ = previous

    async def prepare(*_args, **_kwargs):
        session.events.append(("prepare",))
        raise primary

    session.prepare = prepare
    session.retirement_failure = retirement

    class Delivery(RecordingDelivery):
        def __init__(self):
            super().__init__()
            self.failure = None
            self.retire_calls = 0

        async def retire(self):
            self.retire_calls += 1
            if self.failure is not None:
                raise self.failure
            await super().retire()

    delivery = Delivery()
    transport = Transport(reply(observation()), reply(response()), reply(progress_response()))
    settings = dict(delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=.01,
                    delivery_retry_max_seconds=.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1, task_settle_seconds=1)
    if stage in ("graph-admission", "graph-controller-delivery"):
        owner = live_controller(api(transport, probe.scope.api_origin), probe, AttemptClaims.live(), session, **settings)
    else:
        owner = live_executor(api(transport, probe.scope.api_origin), probe, claim, session, **settings)

    original_primary_cause = primary.__cause__
    original_retirement_cause = retirement.__cause__
    if variant == "multiple" and stage == "graph-run":
        delivery.failure = delivery_failure
    if stage == "graph-admission":
        def refuse(_pending):
            raise primary
        owner._reserve_start = refuse
        operation = owner.run(record())
    elif stage == "graph-controller-delivery":
        session.retirement_failure = None
        delivery.failure = delivery_failure
        if variant == "cancel":
            delivery.failure = primary
        operation = owner.settle()
    elif stage == "graph-settle":
        session.retirement_failure = OSError(errno.EIO, "PRIVATE first retirement")
        try:
            await owner.run(acknowledged)
        except ExecutionFailed as failed:
            assert failed is owner.last_failure and failed.cause is primary
        else:
            raise AssertionError("fixture execution did not retain its initial failure")
        session.retirement_failure = retirement
        delivery.failure = delivery_failure
        operation = owner.settle()
    else:
        operation = owner.run(acknowledged)

    try:
        await operation
    except asyncio.CancelledError as caught:
        assert variant == "cancel" and caught is primary
        assert caught.__cause__ is owner.last_failure
        assert owner.last_failure.interruption_cause is previous
        assert previous in failure_graph(caught)
    except BaseExceptionGroup as caught:
        nodes = failure_graph(caught)
        assert owner.last_failure in nodes
        assert caught is not primary and caught is not retirement and caught is not delivery_failure
        assert primary.__cause__ is original_primary_cause
        assert retirement.__cause__ is original_retirement_cause
        if stage != "graph-controller-delivery":
            assert retirement in nodes
        if variant == "multiple" or stage in ("graph-settle", "graph-controller-delivery"):
            assert delivery_failure in nodes
        if stage not in ("graph-settle", "graph-controller-delivery"):
            assert primary in nodes
        evidence = json.dumps(failure_evidence(caught, secrets=()))
        assert '"independent cleanup"' in evidence or stage == "graph-controller-delivery"
        if type(retirement) is OSError and stage != "graph-controller-delivery":
            assert '"errno": 5' in evidence
    else:
        raise AssertionError("owned failure did not escape")
    assert not delivery.items
    assert len(transport.calls) <= 3
    print(stage + "/" + variant + ": outward evidence retains independent cleanup and original causes")


if __name__ == "__main__" and len(sys.argv) == 3:
    if sys.argv[1].startswith("graph-"):
        operation = graph_child
    elif sys.argv[1] in ("retirement", "handoff", "condition"):
        operation = owned_port_child
    else:
        operation = child
    asyncio.run(operation(*sys.argv[1:]))
elif __name__ == "__main__":
    for owner_kind in ("executor", "controller"):
        for failure_kind in ("system-exit", "keyboard-interrupt", "ordinary", "cancel"):
            result = subprocess.run([sys.executable, __file__, owner_kind, failure_kind],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, (owner_kind, failure_kind, result.returncode, result.stdout, result.stderr)
            assert result.stderr == "", result.stderr
            print(result.stdout, end="")
