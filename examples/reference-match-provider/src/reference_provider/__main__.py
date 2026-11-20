"""Run two reference queries using the selected deployment."""

import asyncio
import sys

from .application import run_queries
from .provider import open_provider
from .science import VectorInput


async def run_demo() -> None:
    async def print_report(payload: bytes) -> None:
        # Flushed stdout is local consumption, not durable publication.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    async with open_provider() as provider:
        await run_queries(
            provider=provider,
            candidates=(VectorInput(values=(3, 4, 0, 0)), VectorInput(values=(0, 0, 1, 1))),
            receive=print_report,
        )


if __name__ == "__main__":
    asyncio.run(run_demo())
