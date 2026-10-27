"""A separate installed-package process contends for the actual lifetime lock."""

import asyncio
from pathlib import Path
import sys

from provider_core.attempt_record import RecoveryScope
from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointStore
from provider_core.lifetime import ProviderLifetime


async def checkpoint(root):
    store = CheckpointStore(
        root, scope=RecoveryScope("https://api.example", "provider:example", "lane:test", "generation:one"),
        limits=CheckpointLimits(2, 300000), operation_seconds=1,
    )
    try:
        try:
            await store.open()
        except CheckpointFailed as failure:
            if not isinstance(failure.cause, BlockingIOError):
                raise
            print("blocked", flush=True)
        else:
            print("acquired", flush=True)
    finally:
        await store.close()


def main():
    mode, root = sys.argv[1], Path(sys.argv[2])
    if mode == "checkpoint":
        asyncio.run(checkpoint(root))
        return
    lifetime = ProviderLifetime(root)
    try:
        lifetime.open()
    except BlockingIOError:
        print("blocked", flush=True)
    else:
        print("acquired", flush=True)
        if mode == "hold":
            sys.stdin.read()
    finally:
        lifetime.close()


if __name__ == "__main__":
    main()
