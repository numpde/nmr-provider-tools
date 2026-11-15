"""Compose and run the local example; no Jobs service or API is implied."""

from __future__ import annotations

import asyncio
import sys

from provider_core.hosting import (
    NotAdmitted, ResultConsumption,
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
    async with open_provider(result_bytes=8192) as provider:
        analysis = None
        workflow = None
        causes = []

        async def print_report(payload: bytes) -> None:
            # Flushed stdout is this demo's consumer, not a durable report queue.
            sys.stdout.buffer.write(payload + b"\n")
            sys.stdout.buffer.flush()

        try:
            workflow = provider.create_workflow(
                spectrum=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
            )
            analysis = workflow.execution
            started = workflow.start()
            if isinstance(started, NotAdmitted):
                raise RuntimeError(f"Baseline admission refused: {started.code.value}: {started.message}")
            async with asyncio.timeout(15):
                result = await workflow.finish()
            if isinstance(result, NotAdmitted):
                raise RuntimeError(f"Peak admission refused: {result.code.value}: {result.message}")
            consumption = ResultConsumption(
                operation=workflow.peaks.operation,
                result=result.require_result(),
                receive=print_report,
            )
            async with consumption:
                async with asyncio.timeout(15):
                    await consumption.consume()
        except BaseException as cause:
            causes.append(cause)
        finally:
            if analysis is not None:
                analysis.request_stop()
            if workflow is not None and workflow.baseline_release is not None:
                try:
                    async with asyncio.timeout(15):
                        await observe_cleanup(control=workflow.baseline_release)
                except BaseException as cause:
                    causes.append(cause)
            # Preserve analysis cleanup failures; host retirement follows on scope exit.
            if analysis is not None:
                try:
                    control = analysis.close()
                    async with asyncio.timeout(15):
                        await observe_cleanup(control=control)
                except BaseException as cause:
                    causes.append(cause)
        if len(causes) == 1:
            raise causes[0]
        if causes:
            raise BaseExceptionGroup("The example and its cleanup reported failures.", causes)


if __name__ == "__main__":
    asyncio.run(run_demo())
