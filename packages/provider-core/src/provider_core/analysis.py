"""Binding-owned scientific outcomes and the asynchronous per-Attempt session.

The split follows NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py::prepare_execution/execute_prepared and
runner_session.py::generate. Its channel retirement does not prove process exit;
each consumer must qualify its actual worker/controller retirement contract.
"""

from provider_core.container_guard import require_container

require_container()

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from provider_core.attempt_record import AcknowledgedAttempt, ProtectedEvidence
from provider_core.job_input import VerifiedJobInput
from provider_core.runner_session import RunnerSession

ReportCondition = Callable[[str | None], Awaitable[None]]


class AnalysisOutcomeUnresolved(RuntimeError):
    """No publishable outcome was established; retain this Attempt's live owner.

    Chain the original exception when one exists. This marker authorizes neither
    FAIL, scientific replay nor claim release. Provider resource readiness and
    the decision to continue unrelated work remain with the binding.
    """


@dataclass(frozen=True, slots=True)
class AnalysisSuccess:
    result_schema_id: str
    content: bytes = field(repr=False)
    evidence: ProtectedEvidence = field(repr=False)


@dataclass(frozen=True, slots=True)
class AnalysisFailure:
    """A binding-classified public failure, not a catch-all for runtime exceptions."""

    code: str
    message: str = field(repr=False)
    evidence: ProtectedEvidence = field(repr=False)


class ObservedWorkerCrash(RuntimeError):
    """Original native exit evidence supplied by the current scientific owner.

    Construction does not authorize FAIL. Core must still admit this exact
    Attempt, phase and owner before selecting a report. Cause and evidence are
    protected diagnostics; public wording uses only the phase and exit status.
    """

    def __init__(self, acknowledged, *, phase, owner, runner, evidence, cause):
        if (type(acknowledged) is not AcknowledgedAttempt or phase not in ("preparing", "running")
                or owner is None or type(runner) is not RunnerSession or type(evidence) is not ProtectedEvidence
                or (cause is not None and not isinstance(cause, BaseException))):
            raise ValueError("Observed crash requires its actual admission, scientific owner, phase, native session and bounded evidence")
        original = runner.original_exit
        if original is None:
            raise ValueError("Cannot classify a worker crash without an original exit observed before local interruption")
        self.acknowledged, self.phase, self.owner = acknowledged, phase, owner
        self.runner, self.native_exit = runner, original
        self.evidence, self.cause = evidence, cause
        super().__init__(
            f"Original worker exit observed (return code {original.returncode}); declared scientific phase: {phase}."
        )

    @classmethod
    def from_session(cls, acknowledged, *, phase, owner, runner, evidence, cause):
        return cls(acknowledged, phase=phase, owner=owner, runner=runner, evidence=evidence, cause=cause)

    def public_failure(self):
        # A signal number does not establish why that signal was sent.
        phase = {"preparing": "preparation", "running": "calculation"}[self.phase]
        code = self.native_exit.returncode
        status = f"signal {-code}" if code < 0 else f"exit status {code}"
        return AnalysisFailure(
            "worker_crashed", f"Worker exited unexpectedly during {phase} ({status}). No verified outcome was returned.",
            self.evidence,
        )


@dataclass(frozen=True, slots=True, eq=False)
class PreparedAnalysis:
    """Session-owned handle whose input identity digest must precede dispatch.

    Identity describes the admitted candidate without source bytes or secrets.
    This instance grants no replay authority: the session accepts it by identity
    once, while its live worker allocation remains owned by that session.
    """

    identity: ProtectedEvidence = field(repr=False)

    def __post_init__(self):
        if type(self.identity) is not ProtectedEvidence:
            raise ValueError("Cannot prepare analysis: protected candidate identity is required before execution.")


class AnalysisSession(Protocol):
    """One bounded async owner existing before preparation can acquire resources.

    Preparation binds the Attempt, current generation and exact admitted inputs to
    a live handle. Execute verifies that handle belongs to this session and consumes
    it before work. Core retains its candidate identity digest, never the live authority.
    Calls must yield while workers run and impose their own aggregate deadlines.
    Session I/O must not share the executor's single-exchange API transport:
    server observation continues concurrently while preparation or execution runs.

    Each phase receives its own awaited condition callable. Supply only reviewed
    public codes or None to clear a condition; never derive codes from exception,
    input or endpoint text. Grammar admission cannot establish public provenance.
    Core binds the Attempt and phase,
    serializes API effects and confirms the receipt before the await returns.
    Propagate reporting failure before dependent work; core also refuses the next
    phase or publication if a binding catches that failure. A retained callable
    expires when its phase finishes or core stops observing that phase. Bindings
    own and await any child work. Calls are sequential; detached or concurrent
    reporting is outside this contract.

    Retire must observe that this exact Attempt's work stopped and required grants
    were released, including partially prepared or interrupted work. It returns
    None only after confirmation, otherwise raises. Warm worker reuse additionally
    requires the binding's qualified quiescence policy; core grants no such reuse.
    A stop request, closed channel or cancelled Python task is not confirmation.

    Raise AnalysisOutcomeUnresolved when no result or reviewed public failure can
    be established. Preserve its original cause; never return it as an outcome.
    Core retains ownership without fabricating FAIL; original retirement must be
    confirmed before continuation.
    """

    async def prepare(
        self, acknowledged: AcknowledgedAttempt, instructions: VerifiedJobInput, *, report_condition: ReportCondition
    ) -> PreparedAnalysis | AnalysisFailure: ...

    async def execute(
        self, prepared: PreparedAnalysis, *, report_condition: ReportCondition
    ) -> AnalysisSuccess | AnalysisFailure: ...

    async def retire(self) -> None: ...


async def retire_session(session):
    """Confirm the protocol result for both pre-start and execution cleanup."""
    result = await session.retire()
    if result is not None:
        raise TypeError("Session retirement must return None after confirmed cleanup, otherwise raise")
