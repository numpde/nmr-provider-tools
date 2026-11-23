"""Select execution at startup and own the two independent scientific stages."""

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import pathlib
import reprlib
import tomllib

from provider_core.contribution import CallBinding, ref
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.execution_setup import ExecutionSettings
from provider_core.hosting import Invocation, OperationPolicy, require_capacity_partition
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution

from .application import PeakWorkflow
from .binding import BaselineCorrector, PeakDetector
from .process_factory import construct_baseline, construct_peaks
from .science import Spectrum


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain independent stage owners and class-bound operation declarations."""

    baseline_host: InProcessExecutionHost | ProcessExecutionHost
    peak_host: InProcessExecutionHost | ProcessExecutionHost
    baseline_corrector: HostedContribution
    peak_detector: HostedContribution
    baseline_call: CallBinding
    peak_call: CallBinding
    operation_policy: OperationPolicy

    def create_workflow(self, *, spectrum: Spectrum) -> PeakWorkflow:
        """Encode before reserving either stage; the caller closes both analyses."""
        spectrum_payload = self.baseline_call.input_codec.encode(value=spectrum)
        baseline_analysis = self.baseline_host.open_analysis(allocations=frozenset({self.baseline_corrector.allocation}))

        try:
            peak_analysis = self.peak_host.open_analysis(allocations=frozenset({self.peak_detector.allocation}))
        except BaseException as failure:
            try:
                baseline_analysis.close()
            except BaseException as cleanup_failure:
                raise BaseExceptionGroup("Opening peak execution and closing baseline execution failed.", [failure, cleanup_failure])

            raise

        return PeakWorkflow(
            baseline_analysis_executor=baseline_analysis,
            peak_analysis_executor=peak_analysis,
            baseline_invocation=Invocation(
                target=ref(call=self.baseline_call),
                payload=spectrum_payload,
                states={},
                reservation=baseline_analysis.reservation(allocation=self.baseline_corrector.allocation),
                policy=self.operation_policy,
            ),
            peak_detection_call_ref=ref(call=self.peak_call),
            peak_detection_reservation_ref=peak_analysis.reservation(allocation=self.peak_detector.allocation),
            operation_policy=self.operation_policy,
        )


def _stage_options(execution: Mapping) -> dict[str, dict]:
    """Check static stage shares before constructing hosts or taking sessions."""
    total = execution["host"]
    shares = execution["stage_capacity"]
    stage_names = set(shares)
    expected_stage_names = {"baseline", "peaks"}

    if stage_names != expected_stage_names:
        raise ValueError(f"{reprlib.repr(stage_names)=} ≠ {expected_stage_names=}")

    require_capacity_partition(total=total, shares=shares)

    # Progress history is bounded per operation; deadlines are per-host policy.
    return {stage: {**total, **share} for stage, share in shares.items()}


@asynccontextmanager
async def open_provider(
    *,
    # silent-defaults: allow-contract -- Only process startup needs deployment-owned sessions; thread execution accepts none.
    process_sessions: Mapping[str, RunnerSession] | None = None,
) -> AsyncIterator[Provider]:
    """Apply one explicit capacity partition on either startup-selected backend."""
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    execution = provider_config["execution"]
    execution_settings = ExecutionSettings(execution)
    backend = execution_settings.backend
    stage_options = _stage_options(execution)

    execution_settings.require_sessions(process_sessions=process_sessions, expected_keys=stage_options.keys())

    async with AsyncExitStack() as lifetime:
        hosts = {}
        hosted = {}

        for (stage, contribution_type, constructor) in (
            ("baseline", BaselineCorrector, construct_baseline),
            ("peaks", PeakDetector, construct_peaks),
        ):
            # silent-defaults: allow-contract -- Thread stages have no runner; process inventory was checked before startup
            process_session = process_sessions[stage] if backend == "process" else None

            (hosts[stage], hosted[stage]) = await lifetime.enter_async_context(
                execution_settings.open_contribution(
                    contribution_type=contribution_type,
                    constructor=constructor,
                    startup=provider_config["science"][stage]["settings"],
                    process_session=process_session,
                    host=stage_options[stage],
                ),
            )

        yield Provider(
            baseline_host=hosts["baseline"],
            peak_host=hosts["peaks"],
            baseline_corrector=hosted["baseline"],
            peak_detector=hosted["peaks"],
            baseline_call=hosted["baseline"].call(method=BaselineCorrector.run),
            peak_call=hosted["peaks"].call(method=PeakDetector.run),
            operation_policy=execution_settings.operation_policy,
        )
