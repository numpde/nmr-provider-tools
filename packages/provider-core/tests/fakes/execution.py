"""Recording async session: proves core ordering, not actual worker retirement."""

import asyncio

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.payload import EncodedPayload
from tests.fakes.outcomes import BytesCodec
from provider_core.attempt_record import ProtectedEvidence, acknowledge_start
from provider_core.ownership import AttemptClaims
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF


class UnexpectedCondition:
    """Standalone scientific fixtures assume permission but emit no conditions.

    Actual controller/stop authority is exercised by the composed lifecycle tests.
    This stateless fake is never a production permission fallback.
    """

    async def __call__(self, _condition_code):
        raise AssertionError("This scientific fixture does not report conditions")

    def require_execution(self):
        pass


# repo-check: allow-module-constant
unexpected_condition = UnexpectedCondition()


class LiveAttemptProbe:
    """Observe the actual live owner rather than copying its lifecycle state."""

    def __init__(self):
        self.scope = scope()
        self.executor = self.controller = None

    @property
    def records(self):
        if self.controller is not None:
            claim = self.controller._claim

            if (claim is None) or (not claim.active):
                return ()

            operation = self.controller.operation
        else:
            operation = self.executor

        checkpoint = getattr(operation, "_checkpoint", None)

        if checkpoint is None:
            return ()

        return (checkpoint,)

    def require_execution_ready(self, claim, executor, acknowledged):
        if not (executor is self.executor):
            raise AssertionError("Only the original live executor can enter science")

    def require_claim_release_ready(self, claim):
        if not (claim.operation_owner is None):
            raise AssertionError("The original operation must settle before release")


async def live_attempt():
    pending = record()
    acknowledged = acknowledge_start(pending, admission())
    probe = LiveAttemptProbe()
    claim = AttemptClaims.live().claim_live(pending, lifecycle_owner=probe)
    owner = object()

    claim.begin(owner)
    claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
    claim.finish(owner)

    return (probe, acknowledged, claim)


def live_executor(api, probe, claim, session, **settings):
    from provider_core.execution import AttemptExecutor

    executor = AttemptExecutor.live(api, claim, session, scope=probe.scope, **settings)
    probe.executor = executor

    return executor


def live_controller(api, probe, claims, session, **settings):
    from unittest.mock import Mock
    from provider_core.controller import AttemptController, LiveStartPolicy

    controller = AttemptController.live(
        api,
        claims,
        session,
        scope=probe.scope,
        stopping=asyncio.Event(),
        start_policy=LiveStartPolicy(0.001, Mock(), Mock()),
        **settings,
    )

    probe.controller = controller

    return controller


class Session:
    def __init__(self, probe):
        self.probe = probe
        self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", b"scripted candidate"))
        self.events = []
        self.preparation = self.handle
        self.outcome = AnalysisSuccess(
            result=EncodedPayload(
                codec=BytesCodec(format="result.v1"),
                value=b"result bytes",
            ),
            evidence=ProtectedEvidence("evidence.v1", b"private evidence"),
        )
        self.retirement_failure = None

    # Match the required session port; this recording fixture emits no conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        self.events.append(("prepare", self.probe.records[0], instructions))

        if acknowledged.start.input != instructions.identity:
            raise AssertionError("substituted input")

        return self.preparation

    # Match the required session port; this recording fixture emits no conditions.
    async def execute(self, prepared, *, report_condition):  # noqa: ARG002
        if not (prepared is self.handle):
            raise AssertionError("foreign prepared handle")

        self.events.append(("execute", self.probe.records[0]))

        return self.outcome

    async def retire(self):
        self.events.append(("retire",))

        if not (self.retirement_failure is None):
            raise self.retirement_failure


class PausedSession(Session):
    """Expose an in-flight phase so policy tests observe state before completion."""

    def __init__(self, store, phase):
        super().__init__(store)
        self.phase = phase
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def _pause(self, phase):
        if self.phase == phase:
            self.paused.set()
            await self.release.wait()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        prepared = await super().prepare(acknowledged, instructions, report_condition=report_condition)

        await self._pause("prepare")

        return prepared

    async def execute(self, prepared, *, report_condition):
        outcome = await super().execute(prepared, report_condition=report_condition)

        await self._pause("execute")

        return outcome


async def execute_with_watchdog(executor, acknowledged):
    """Keep a polling regression from hanging tests on a paused session."""
    task = asyncio.create_task(executor.run(acknowledged))

    try:
        await asyncio.wait((task,), timeout=(execution_timeout_seconds := 3))

        if not task.done():
            raise TimeoutError(f"Test execution did not finish within {execution_timeout_seconds=}")

        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=(cancellation_timeout_seconds := 1))

            if not task.done():
                raise TimeoutError(f"Test execution did not finish within {cancellation_timeout_seconds=} after cancellation was requested")

        if not task.cancelled():
            task.exception()
