"""Shrink interleavings of independent live Attempts sharing SDK capacity.

Real controllers, Interpreters and supervised model-HTTP children remain in use.
Only the external TLS replies, scientific completion and retirement are gated.
The deterministic complete traces live in test_model_reply_permission.py.
"""

import asyncio
from contextlib import AsyncExitStack
import json
import os
from unittest.mock import Mock, patch

from hypothesis import note, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from provider_core.analysis import AnalysisOutcomeUnresolved
from provider_core.controller import ControllerFailed, LiveStartPolicy, StartDeferred
from provider_core.execution import AttemptExecutor, ExecutionStopped
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from tests.fakes.api import reply, signature_window_problem
from tests.fakes.checkpoint_execution import CausalDelivery
from tests.fakes.attempts import start_response
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer
from tests.integration.chat.adversarial.test_model_reply_permission import (
    ObservedAPI, PreparedExecutionGateSession, ReplyRun,
)


class _UnknownOutcome(PreparedExecutionGateSession):
    """Preserve an original scientific dispatch whose result is uncertain."""

    async def execute(self, prepared, *, report_condition):
        await super().execute(prepared, report_condition=report_condition)
        raise AnalysisOutcomeUnresolved("The original scientific result is unconfirmed")


class _ControlledStart(ObservedAPI):
    """The fake API controls the original START reply, not controller policy."""

    def __init__(self, job, reference):
        super().__init__(job, reference)
        self.start_received = asyncio.Event()
        self.release_start = asyncio.Event()
        self.disposition = None
        self.sent_disposition = None

    async def exchange(self, request, *, maximum_bytes, owner=None):
        if request.url.path.endswith("/start"):
            self.calls.append((request, maximum_bytes))
            self.start_received.set()
            await self.release_start.wait()
            self.sent_disposition = self.disposition
            if self.disposition == "uncertain":
                # A 503 does not establish whether START committed; no science
                # is authorized without the exact matching acknowledgement.
                return reply(b"maintenance", status=503)
            if self.disposition == "uncommitted":
                return reply(signature_window_problem(), status=401)
            if self.disposition == "accepted":
                return reply(start_response(
                    job_ref=self.job, execution_attempt_ref=self.reference))
            raise AssertionError("START gate released without a selected API disposition")
        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)


class LifecycleOrders(RuleBasedStateMachine):
    """A and B own separate model children and share one two-entry claim pool."""

    def __init__(self):
        super().__init__()
        self.runner = asyncio.Runner()
        self.trace = []
        self.started = set()
        self.local_stops = set()
        self.verified_stops = {}
        try:
            self.runner.run(self._open())
        except BaseException:
            self.runner.close()
            raise

        # Observe the precise SDK-owned classification of a correlated read.
        # The original method still performs all permission decisions.
        original_check = AttemptExecutor._require_observed_active

        def observed_check(executor, observation):
            try:
                return original_check(executor, observation)
            except ExecutionStopped as stopped:
                for name, case in self.cases.items():
                    if case.controller.operation is executor:
                        self.verified_stops.setdefault(
                            name, (stopped, case.session.science.executions))
                        break
                raise

        self._observer = patch.object(
            AttemptExecutor, "_require_observed_active", observed_check)
        self._observer.start()

    async def _open(self):
        self.stack = AsyncExitStack()
        self.claims = AttemptClaims.live(limits=LiveAttemptLimits(
            maximum_attempts=2, maximum_control_bytes=262144,
            maximum_pending_terminal_bytes=4194304,
        ))
        self.cases = {}
        self.replies = {}
        for name in ("a", "b"):
            released = asyncio.Event()

            async def model_reply(_request, _body, *, gate=released, identity=name):
                await gate.wait()
                return json.dumps({"choices": [{"message": call(identity, "prepare", {}),
                                                "finish_reason": "tool_calls"}]}).encode()

            peer = await self.stack.enter_async_context(
                TlsPeer("success", response_factory=model_reply))
            self.cases[name] = ReplyRun(
                peer, self.claims, "job:" + name,
                _UnknownOutcome if name == "a" else PreparedExecutionGateSession,
                transport_type=_ControlledStart,
                start_policy=LiveStartPolicy(1, Mock(), Mock(), maximum_replays=0),
                delivery=CausalDelivery(),
            )
            self.replies[name] = released
        self.tasks = {}

    async def _progress(self):
        # Allow ready callbacks to run; no elapsed delay controls the fault.
        await asyncio.sleep(0)

    def _act(self, action, name, callback):
        self.trace.append((action, name))
        self.runner.run(callback())

    @rule(name=st.sampled_from(("a", "b")))
    def start(self, name):
        if name in self.started:
            return
        self.started.add(name)

        async def launch():
            case = self.cases[name]
            self.tasks[name] = asyncio.create_task(case.controller.run(case.pending))
            await self._progress()

        self._act("start", name, launch)

    @rule(name=st.sampled_from(("a", "b")),
          disposition=st.sampled_from(("accepted", "uncertain", "uncommitted")))
    def decide_original_start(self, name, disposition):
        if name not in self.started:
            return
        transport = self.cases[name].transport
        # Admission can be refused or locally stopped before a START exists.
        # A generated reply may only follow the actual observed original send.
        if transport.release_start.is_set() or not transport.start_received.is_set():
            return

        async def release():
            transport.disposition = disposition
            transport.release_start.set()
            await self._progress()

        self._act("release_original_start_" + disposition, name, release)

    @rule(name=st.sampled_from(("a", "b")),
          stop=st.sampled_from(("cancelled", "expired")))
    def withdraw_permission(self, name, stop):
        if name not in self.started or self.cases[name].transport.stop:
            return

        async def revoke():
            self.cases[name].transport.stop = (
                {"job_state": "cancelled"} if stop == "cancelled"
                else {"state": "expired"}
            )
            await self._progress()

        self._act("server_" + stop, name, revoke)

    @rule(name=st.sampled_from(("a", "b")))
    def release_model_reply(self, name):
        if self.replies[name].is_set():
            return

        async def release():
            self.replies[name].set()
            await self._progress()

        self._act("release_model_reply", name, release)

    @rule(name=st.sampled_from(("a", "b")))
    def release_scientific_preparation(self, name):
        science = self.cases[name].session.science
        if science.release.is_set():
            return

        async def release():
            science.release.set()
            await self._progress()

        self._act("release_scientific_preparation", name, release)

    @rule(name=st.sampled_from(("a", "b")))
    def release_accepted_execution(self, name):
        session = self.cases[name].session
        if session.release_execution.is_set():
            return

        async def release():
            session.release_execution.set()
            await self._progress()

        self._act("release_accepted_execution", name, release)

    @rule(name=st.sampled_from(("a", "b")))
    def inject_independent_retirement_failure(self, name):
        session = self.cases[name].session
        if (session.release_retirement.is_set()
                or session.retirement_completed
                or session.retirement_failure is not None):
            return

        async def inject():
            # The original owner reports a separate cleanup failure. It
            # cannot be interpreted as success or authorize another attempt.
            session.retirement_failure = RuntimeError(
                "An original scientific resource did not retire")
            await self._progress()

        self._act("original_retirement_failed", name, inject)

    @rule(name=st.sampled_from(("a", "b")))
    def confirm_session_retirement(self, name):
        session = self.cases[name].session
        if session.release_retirement.is_set():
            return

        async def release():
            session.release_retirement.set()
            await self._progress()

        self._act("confirm_session_retirement", name, release)

    @rule(name=st.sampled_from(("a", "b")),
          decision=st.sampled_from(("accepted", "full", "retryable", "lost", "rejected")))
    def resolve_local_delivery(self, name, decision):
        delivery = self.cases[name].delivery
        if not delivery.awaiting_answer:
            return

        async def respond():
            delivery.answer(decision)
            await self._progress()

        self._act("local_delivery_" + decision, name, respond)

    @rule(name=st.sampled_from(("a", "b")))
    def request_local_stop(self, name):
        if name not in self.started or name in self.local_stops:
            return
        self.local_stops.add(name)

        async def withdraw():
            # Local stopping is not a verified remote cancellation or a
            # terminal-delivery acknowledgement.
            self.cases[name].controller.request_stop()
            await self._progress()

        self._act("local_stop", name, withdraw)

    @rule(name=st.sampled_from(("a", "b")))
    def join_completed_owner(self, name):
        task = self.tasks.get(name)
        if task is None or not task.done():
            return

        async def join():
            case = self.cases[name]
            result, = await asyncio.gather(task, return_exceptions=True)
            if isinstance(result, ControllerFailed) and case.session.retirement_completed:
                observed = await case.controller.settle_revocation(result)
                if observed is not None:
                    assert case.controller.shutdown_ready
                    assert not self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
            elif type(result) is StartDeferred:
                assert (case.session.science.preparations, case.session.science.executions) == (0, 0)
                if result.may_have_committed:
                    # A failed START exchange is not a grant to prepare work,
                    # and positive cleanup cannot release uncertain custody.
                    assert case.transport.sent_disposition == "uncertain"
                    assert self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
                    await case.controller.settle()
                    assert self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
                else:
                    assert not self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
            elif type(result) is DeliveryAcceptedCheckpoint:
                assert case.controller.shutdown_ready
                assert not self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)

        self._act("join_original_owner", name, join)

    @invariant()
    def preserve_exclusive_ownership_and_once_only_science(self):
        async def inspect():
            owners = set(self.claims.live_owners())
            for name, case in self.cases.items():
                session = case.session
                science = session.science
                assert case.start_count <= 1
                assert session.preparations <= 1
                assert session.dispatches <= 1
                assert science.preparations <= 1
                assert science.executions <= 1
                assert len(case.peer.requests) <= 1
                delivery = case.delivery
                if delivery.offers:
                    original_terminal = delivery.offers[0]
                    assert all(item is original_terminal for item in delivery.offers), (
                        "A delivery retry replaced the original logical terminal")
                    assert all(item.key == original_terminal.key
                               and item.body == original_terminal.body
                               and item.body_sha256 == original_terminal.body_sha256
                               for item in delivery.offers)
                    assert len(delivery.offers) <= delivery._answered + 1
                    if not delivery.accepted.is_set():
                        # Selection, a rejected reply or possible local storage
                        # is not the SDK's verified acceptance checkpoint.
                        assert not case.controller.shutdown_ready
                        assert not case.controller.operation.delivery_accepted
                    elif case.controller.operation.delivery_accepted:
                        assert case.controller._claim._pending_terminal_bytes == 0, (
                            "The exact relay acknowledgement did not free terminal capacity")
                assert (session.accepted_handle is None
                        or session.accepted_handle is science.handle)
                if session.execution_entered.is_set():
                    # The SDK accepted the one original handle before calling
                    # the session's execute operation.
                    assert session.accepted_handle is science.handle
                    assert science.preparations == 1
                if science.retired_handle is not None:
                    assert science.retired_handle is science.handle
                if case.transport.sent_disposition == "uncertain":
                    assert session.preparations == 0
                    assert science.preparations == 0
                    assert science.executions == 0
                if name in self.verified_stops:
                    stopped, effects_at_stop = self.verified_stops[name]
                    assert stopped.observation.job_ref == case.pending.input.job_ref
                    assert science.executions == effects_at_stop, (
                        "New scientific work entered after the SDK verified server revocation")
                owned = self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
                assert owned == (case.controller in owners)
                if session.retiring.is_set() and not session.retirement_completed and case.start_count:
                    assert owned, "Retirement is pending but its original claim was released"
                if (session.retirement_failure is not None and session.retiring.is_set()
                        and session.release_retirement.is_set()):
                    assert owned, "Independent retirement failure released the original claim"
                if not owned and case.start_count:
                    assert session.retirement_completed, "Claim released before positive session retirement"
            assert len(owners) <= 2

        self.runner.run(inspect())
        note(f"Causal commands: {self.trace!r}")

    def teardown(self):
        async def close():
            for name, case in self.cases.items():
                self.replies[name].set()
                case.delivery.release_for_teardown()
                case.transport.release_start.set()
                # Teardown must select a real result for a previously dispatched
                # START; releasing a gate with no response would hide a failure.
                if case.transport.disposition is None:
                    case.transport.disposition = "accepted"
                case.session.science.release.set()
                case.session.release_execution.set()
                case.session.release_retirement.set()
            for name, task in self.tasks.items():
                if not task.done():
                    self.cases[name].controller.request_stop()
            if self.tasks:
                await asyncio.wait_for(
                    asyncio.gather(*self.tasks.values(), return_exceptions=True), 12)
            await self.stack.aclose()

        try:
            self.runner.run(close())
        finally:
            self._observer.stop()
            self.runner.close()


# silent-defaults: allow-contract -- Normal test discovery uses the bounded campaign; the explicit fuzz lane selects extended.
_PROFILE = os.environ.get("PROVIDER_CORE_FUZZ_PROFILE", "default")
if _PROFILE not in ("default", "extended"):
    raise ValueError("Lifecycle fuzz profile must be default or extended")
TestLifecycleOrders = LifecycleOrders.TestCase
TestLifecycleOrders.settings = settings(
    max_examples=32 if _PROFILE == "default" else 1000,
    stateful_step_count=40 if _PROFILE == "default" else 150,
    derandomize=True, database=None, deadline=None, print_blob=True,
)
