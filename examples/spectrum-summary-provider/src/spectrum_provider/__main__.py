"""Compose and run the local example; no Jobs service or API is implied."""

from __future__ import annotations

import asyncio
import sys

from provider_core.failure_collection import FailureCollector

from provider_core.hosting import (
    NotAdmitted,
    ResultConsumption,
    observe_cleanup,
)

from .provider import open_provider
from .science import Spectrum


async def run_demo() -> None:
    """Consume one report, then close analysis before the SDK host context exits.

    Explicit result/analysis observations retain their caller timeouts. The outer
    host context joins original retirement instead of abandoning pending cleanup
    when an observer cap expires.
    """

    async with open_provider() as provider:
        analysis_executor = None
        workflow = None
        failure_collector = FailureCollector(message="The example and its cleanup reported failures.")

        async def print_report(payload: bytes) -> None:
            # Flushed stdout is this demo's consumer, not a durable report queue.
            sys.stdout.buffer.write(payload + b"\n")

            sys.stdout.buffer.flush()

        with failure_collector.capture():
            workflow = provider.create_workflow(
                spectrum=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
            )

            analysis_executor = workflow.analysis_executor
            baseline_admission = workflow.start()

            # repo-check: retain-type-guard -- A refused baseline submission is reported rather than treated as an operation
            if isinstance(baseline_admission, NotAdmitted):
                raise RuntimeError(f"Baseline admission refused: {baseline_admission=!s}")

            async with asyncio.timeout(15):
                peak_observation = await workflow.finish()

            # repo-check: retain-type-guard -- A refused observation request is reported rather than treated as an operation
            if isinstance(peak_observation, NotAdmitted):
                raise RuntimeError(f"Peak admission refused: {peak_observation=!s}")

            result_consumer = ResultConsumption(
                operation=workflow.peak_detection_submission.operation,
                result=peak_observation.require_result(),
                receive=print_report,
            )

            async with result_consumer:
                async with asyncio.timeout(15):
                    await result_consumer.consume()

        if analysis_executor is not None:
            with failure_collector.capture():
                analysis_executor.request_stop()

        if (workflow is not None) and (workflow.baseline_report_release is not None):
            with failure_collector.capture():
                async with asyncio.timeout(15):
                    await observe_cleanup(control=workflow.baseline_report_release)

        # Preserve analysis cleanup failures; host retirement follows on scope exit.
        if analysis_executor is not None:
            with failure_collector.capture():
                control = analysis_executor.close()

                async with asyncio.timeout(15):
                    await observe_cleanup(control=control)

        failure_collector.raise_if_any()


if __name__ == "__main__":
    asyncio.run(run_demo())
