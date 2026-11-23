"""Compose and run the local example; no Jobs service or API is implied."""

import asyncio
from functools import partial
import sys

from provider_core.scientific_invocation import open_invocation

from .provider import open_provider
from .science import Spectrum


async def run_demo() -> None:
    """Print one report after both calls settle, then retire their hosts."""

    async with open_provider() as provider:
        async with open_invocation(
            partial(
                provider.summarize,
                spectrum=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
            ),
        ) as invocation:
            async with asyncio.timeout(15):
                report = await invocation.wait()

            # Flushed stdout is this demo's consumer, not a durable report queue
            sys.stdout.buffer.write(provider.peaks.binding.result_codec.encode(report) + b"\n")

            sys.stdout.buffer.flush()


if __name__ == "__main__":
    asyncio.run(run_demo())
