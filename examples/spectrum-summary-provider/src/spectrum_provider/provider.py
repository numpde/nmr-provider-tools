"""Select execution at startup and own the two independent scientific stages."""

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import pathlib
import reprlib
import tomllib

from provider_core.execution_setup import ExecutionSettings
from provider_core.hosting import require_capacity_partition
from provider_core.runner_session import RunnerSession
from provider_core.scientific_call import ScientificCall

from .binding import BaselineCorrector, PeakDetector
from .process_factory import construct_baseline, construct_peaks
from .science import PeakReport, Spectrum


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Compose typed calls while the enclosing setup owns their two hosts."""

    baseline: ScientificCall[Spectrum, Spectrum]
    peaks: ScientificCall[Spectrum, PeakReport]

    async def summarize(self, *, spectrum: Spectrum) -> PeakReport:
        corrected = await self.baseline(scientific_input=spectrum)

        return await self.peaks(scientific_input=corrected)


def _stage_options(execution: Mapping) -> dict[str, dict]:
    """Check static stage shares before constructing hosts or taking sessions."""
    total = execution["host"]
    shares = execution["stage_capacity"]
    stage_names = set(shares)
    expected_stage_names = {"baseline", "peaks"}

    if not (stage_names == expected_stage_names):
        raise ValueError(f"{reprlib.repr(stage_names)=} ≠ {expected_stage_names=}")

    require_capacity_partition(total=total, shares=shares)

    # Progress history is bounded per operation; deadlines are per-host policy.
    return {stage: {**total, **share} for (stage, share) in shares.items()}


@asynccontextmanager
async def open_provider(
    *,
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
            baseline=execution_settings.bind_call(host=hosts["baseline"], contribution=hosted["baseline"], method=BaselineCorrector.run),
            peaks=execution_settings.bind_call(host=hosts["peaks"], contribution=hosted["peaks"], method=PeakDetector.run),
        )
