"""Prove real durable IPC while execution-controller imports are unavailable."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import importlib
import importlib.abc
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock


class ExecutionUnavailable(importlib.abc.MetaPathFinder):
    blocked = {
        "provider_core.controller", "provider_core.execution", "provider_core.analysis",
        "provider_core.runner", "provider_core.runner_protocol", "provider_core.runner_session",
        "provider_core.recovery", "provider_core.starting", "nmrnet_provider", "nmrnet_inference",
        "magnet_provider", "rdkit", "torch",
    }

    def find_spec(self, fullname, path, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in self.blocked):
            raise ModuleNotFoundError(f"Execution modules are unavailable in this client-only proof: {fullname}")
        return None


async def prove_handoff():
    blocker = ExecutionUnavailable()
    sys.meta_path.insert(0, blocker)
    try:
        importlib.import_module("provider_core.controller")
    # silent-defaults: allow-contract -- This expected refusal proves the execution-module blocker is active.
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("The execution-controller import blocker did not run.")
    client_module = importlib.import_module("provider_core.delivery_client")
    ipc = importlib.import_module("provider_core.delivery_ipc")
    store = importlib.import_module("provider_core.delivery_store")
    attempt = importlib.import_module("provider_core.attempt_record")
    requests = importlib.import_module("provider_core.attempt_request")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "ipc").mkdir(mode=0o700)
        (root / "state").mkdir(mode=0o700)
        queue = store.DeliveryQueue(root / "state", limits=store.QueueLimits(1, 4096, 2, 8192))
        endpoint = client_module.DeliveryEndpoint("https://api.example", "provider:test", root / "ipc/http.sock")
        inbox = ipc.DeliveryInbox(
            queue, (endpoint,), request_seconds=1, on_accepted=Mock(), on_failure=Mock(),
            binding_status=Mock(return_value={"state": "inbox_only", "diagnostics": {}}),
        )
        client = client_module.DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
        await inbox.start()
        try:
            reference = "execution_attempt:sha256:" + "a" * 64
            body = requests.fail_body(reference, "provider_interrupted", "Synthetic client-only qualification") + b"\n"
            item = store.DeliveryOutcome("https://api.example", "provider:test", "job:queue", reference, attempt.TerminalOperation.FAIL, body)
            assert await client.submit(item) == item.acceptance()
            assert queue.next_pending(item.api_origin, item.provider_ref) == item
            assert await client.submit(item) == item.acceptance()
            assert len(queue.inventory(item.api_origin, item.provider_ref)) == 1
            assert client.retirement_confirmed
        finally:
            await client.retire()
            await inbox.close()
            queue.close()
    assert not any(name in sys.modules for name in blocker.blocked)


if __name__ == "__main__":
    asyncio.run(prove_handoff())
