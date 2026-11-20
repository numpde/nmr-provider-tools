"""Binding-owned scientific outcomes and the asynchronous per-Attempt session.

The split follows NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py::prepare_execution/execute_prepared and
runner_session.py::generate. Its channel retirement does not prove process exit;
each consumer must qualify its actual worker/controller retirement contract.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field
from typing import Protocol

from provider_core.attempt_record import AcknowledgedAttempt, ProtectedEvidence
from provider_core.validation.upstream.nmr.v1.job_input import VerifiedJobInput
from provider_core.runner_session import RunnerSession
from provider_core.type_requirements import require_instance, require_type


class ReportCondition(Protocol):
    """One phase's awaited condition reporting and current local permission.

    Call require_execution immediately before committing a binding effect, after
    any intervening await. It samples the actual phase/controller authority; it
    does not fetch server state, reserve future permission or retract submitted
    work. Owned child tasks on the same loop may use this same phase capability.
    """

    async def __call__(self, condition_code: str | None) -> None: ...

    def require_execution(self) -> None: ...


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
        require_type(acknowledged, AcknowledgedAttempt)

        if not ((phase in ("preparing", "running")) and (owner is not None)):
            raise ValueError("Observed crash requires its actual admission, scientific owner, phase, native session and bounded evidence")

        require_type(runner, RunnerSession)

        require_type(evidence, ProtectedEvidence)

        if cause is not None:
            require_instance(cause, BaseException)

        original = runner.original_exit

        if not (original is not None):
            raise ValueError("Cannot classify a worker crash without an original exit observed before local interruption")

        (self.acknowledged, self.phase, self.owner) = (acknowledged, phase, owner)
        (self.runner, self.native_exit) = (runner, original)
        (self.evidence, self.cause) = (evidence, cause)

        super().__init__(
            f"Original worker exit observed with {original.returncode = }; declared scientific {phase = }."
        )

    @classmethod
    def from_session(cls, acknowledged, *, phase, owner, runner, evidence, cause):
        return cls(acknowledged, phase=phase, owner=owner, runner=runner, evidence=evidence, cause=cause)

    def public_failure(self):
        # A signal number does not establish why that signal was sent.
        phase = {"preparing": "preparation", "running": "calculation"}[self.phase]

        return_code = self.native_exit.returncode

        if return_code < 0:
            signal_number = -return_code
            reason = f"Worker exited unexpectedly during {phase = } after receiving {signal_number = }. No verified outcome was returned."
        else:
            reason = f"Worker exited unexpectedly during {phase = } with {return_code = }. No verified outcome was returned."

        return AnalysisFailure(
            "worker_crashed",
            reason,
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
        require_type(self.identity, ProtectedEvidence)


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

    Check report_condition.require_execution() immediately before native work or
    scientific submission, including after awaited preparation or retirement.
    Scheduled child work must check at its actual effect boundary; a check before
    scheduling does not reserve permission across the later task handoff.

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
        self,
        acknowledged: AcknowledgedAttempt,
        instructions: VerifiedJobInput,
        *,
        report_condition: ReportCondition
    ) -> PreparedAnalysis | AnalysisFailure: ...

    async def execute(
        self,
        prepared: PreparedAnalysis,
        *,
        report_condition: ReportCondition
    ) -> AnalysisSuccess | AnalysisFailure: ...

    async def retire(self) -> None: ...


async def retire_session(session):
    """Confirm the protocol result for both pre-start and execution cleanup."""
    retirement_return = await session.retire()

    if not (retirement_return is None):
        raise TypeError("Session retirement must return None after confirmed cleanup, otherwise raise")
