"""Shrink interleavings of independent live Attempts sharing SDK capacity.

Real controllers, Interpreters and supervised model-HTTP children remain in use.
Only the external TLS replies, scientific completion and retirement are gated.
The deterministic complete traces live in test_model_reply_permission.py.
"""

import asyncio
from contextlib import AsyncExitStack
import json
import os

from hypothesis import note, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from provider_core.analysis import AnalysisOutcomeUnresolved
from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer
from tests.integration.chat.adversarial.test_model_reply_permission import (
    HeldRetirementSession, ReplyRun,
)


class _UnknownOutcome(HeldRetirementSession):
    """Preserve an original scientific dispatch whose result is uncertain."""

    async def execute(self, prepared, *, report_condition):
        await super().execute(prepared, report_condition=report_condition)
        raise AnalysisOutcomeUnresolved("The original scientific result is unconfirmed")


class LifecycleOrders(RuleBasedStateMachine):
    """A and B own separate model children and share one two-entry claim pool."""

    def __init__(self):
        super().__init__()
        self.runner = asyncio.Runner()
        self.trace = []
        self.started = set()
        self.runner.run(self._open())

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
            self.cases[name] = ReplyRun(peer, self.claims, "job:" + name,
                                        _UnknownOutcome if name == "a" else HeldRetirementSession)
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

    @rule(name=st.sampled_from(("a", "b")))
    def withdraw_permission(self, name):
        if name not in self.started or self.cases[name].transport.stop:
            return

        async def revoke():
            self.cases[name].transport.stop = {"job_state": "cancelled"}
            await self._progress()

        self._act("server_cancelled", name, revoke)

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
    def confirm_session_retirement(self, name):
        session = self.cases[name].session
        if session.release_retirement.is_set():
            return

        async def release():
            session.release_retirement.set()
            await self._progress()

        self._act("confirm_session_retirement", name, release)

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
                owned = self.claims.owns_job(case.pending.scope, case.pending.input.job_ref)
                assert owned == (case.controller in owners)
                if session.retiring.is_set() and not session.retirement_completed and case.start_count:
                    assert owned, "Retirement is pending but its original claim was released"
                if not owned and case.start_count:
                    assert session.retirement_completed, "Claim released before positive session retirement"
            assert len(owners) <= 2

        self.runner.run(inspect())
        note(f"Causal commands: {self.trace!r}")

    def teardown(self):
        async def close():
            for name, case in self.cases.items():
                self.replies[name].set()
                case.session.science.release.set()
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
