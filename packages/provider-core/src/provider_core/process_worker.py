"""Installed, single-allocation scientific worker for a private process host.

The worker constructs and retains its contribution after an SDK resident owns
its lifetime. Prepared values never leave this process: only SDK references,
encoded inputs and results cross the channel.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import argparse
import asyncio
from collections import OrderedDict
import importlib
import socket
from collections.abc import Mapping

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import CleanupObservation, NotAdmitted, ResultState, settle_cleanup
from provider_core.process_wire import receive_async, send_async
from provider_core.scientific import Contribution


HISTORY_LIMIT = 128


def _installed_member(path: str):
    module_name, separator, member_path = path.partition(":")

    if not separator or not module_name or not member_path or "<locals>" in member_path:
        raise ValueError("Scientific worker requires an installed module:member path")

    value = importlib.import_module(module_name)

    for name in member_path.split("."):
        value = getattr(value, name)

    return value


class _Worker:
    def __init__(self):
        self.host = None
        self.hosted = None
        self.analyses = {}
        self.operations = {}
        self.releases = {}
        self.analysis_cleanups = {}
        self.finished_analyses = OrderedDict()
        self.finished_operations = OrderedDict()

    async def start(self, message: Mapping) -> dict:
        if self.host is not None:
            raise RuntimeError("Scientific worker startup is single-use")

        host = InProcessExecutionHost(**message["host_options"])
        self.host = host
        await host.__aenter__()

        constructor = _installed_member(message["constructor"])
        contribution_type = _installed_member(message["contribution_type"])

        if not issubclass(contribution_type, Contribution):
            raise TypeError("Scientific worker declaration must be a Contribution class")

        startup = message["startup"]

        def construct() -> Contribution:
            return constructor(startup=startup)

        hosted = host.adopt_class(
            contribution_type=contribution_type,
            constructor=construct,
            requirements=frozenset(message["requirements"]),
        )

        self.hosted = hosted
        await host.initialize(allocation=hosted.allocation)

        return {
            "allocation": hosted.allocation,
            "binding_revision": hosted.description.binding_revision,
        }

    def _operation(self, message):
        key = (message["analysis"], message["operation"])
        operation = self.operations.get(key)

        if operation is None:
            raise LookupError("This scientific operation is no longer available in the worker")

        return operation

    async def _finish_analysis(self, analysis_id, observation):
        if not observation.settled:
            return

        self.finished_analyses[analysis_id] = observation
        self.finished_analyses.move_to_end(analysis_id)

        while len(self.finished_analyses) > HISTORY_LIMIT:
            self.finished_analyses.popitem(last=False)

        self.analyses.pop(analysis_id, None)
        self.analysis_cleanups.pop(analysis_id, None)

        for key, operation in list(self.operations.items()):
            if key[0] != analysis_id:
                continue

            current = await operation.observe(after=None, wait_seconds=0)

            if current.result_state is ResultState.AVAILABLE:
                continue

            self.finished_operations[key] = current
            self.finished_operations.move_to_end(key)
            del self.operations[key]

        while len(self.finished_operations) > HISTORY_LIMIT:
            self.finished_operations.popitem(last=False)

    async def command(self, message: Mapping) -> object:
        action = message["action"]

        if action == "start":
            return await self.start(message)

        if self.hosted is None:
            raise RuntimeError("Scientific worker is not initialized")

        if action == "submit":
            invocation = message["invocation"]
            analysis_id = invocation.reservation.analysis
            analysis = self.analyses.get(analysis_id)

            if analysis is None:
                analysis = self.host.open_analysis(
                    analysis_id=analysis_id,
                    allocations=frozenset({self.hosted.allocation}),
                )
                self.analyses[analysis_id] = analysis

            decision = analysis.submit(invocation=invocation)

            if isinstance(decision, NotAdmitted):
                return {"refusal": decision}

            self.operations[(analysis_id, invocation.operation_id)] = decision
            return {"operation": decision.reference}

        if action == "observe":
            key = (message["analysis"], message["operation"])

            if key in self.finished_operations:
                return self.finished_operations[key]

            return await self._operation(message).observe(after=None, wait_seconds=0)

        if action == "read_result":
            return await self._operation(message).read_result(result=message["result"])

        if action == "stop_operation":
            self._operation(message).request_stop()
            return None

        if action == "release_result":
            operation = self._operation(message)
            key = (message["analysis"], message["operation"], message["result"].result_id)
            control = self.releases.get(key)

            if control is None:
                control = operation.release_result(result=message["result"])
                self.releases[key] = control

            observed = await control.observe(after=None, wait_seconds=0)

            if observed.settled:
                self.releases.pop(key, None)

            return observed

        if action == "stop_analysis":
            analysis = self.analyses.get(message["analysis"])

            if analysis is not None:
                analysis.request_stop()

            return None

        if action == "close_analysis":
            analysis_id = message["analysis"]
            analysis = self.analyses.get(analysis_id)

            if analysis is None:
                return self.finished_analyses.get(
                    analysis_id,
                    CleanupObservation(revision=0, settled=True, allowance_expired=False, failures=()),
                )

            control = self.analysis_cleanups.get(analysis_id)

            if control is None:
                control = analysis.close()
                self.analysis_cleanups[analysis_id] = control

            observed = await control.observe(after=None, wait_seconds=0)
            await self._finish_analysis(analysis_id, observed)
            return observed

        if action == "retire":
            return await settle_cleanup(control=self.host.retire())

        raise ValueError(f"Unknown scientific worker action {action!r}")

    async def retire(self):
        if self.host is not None:
            await settle_cleanup(control=self.host.retire())


async def _serve(sock: socket.socket) -> None:
    sock.setblocking(False)
    worker = _Worker()
    retirement_requested = False

    try:
        while True:
            try:
                message = await receive_async(sock)
            except EOFError:
                break

            try:
                outcome = await worker.command(message)
            except BaseException as cause:
                await send_async(sock, {"ok": False, "error": cause})
            else:
                await send_async(sock, {"ok": True, "value": outcome})

            if message["action"] == "retire":
                retirement_requested = True
                break
    finally:
        try:
            if not retirement_requested:
                await worker.retire()
        finally:
            sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve one installed scientific allocation")
    parser.add_argument("--session-fd", required=True, type=int)
    args = parser.parse_args(argv)

    sock = socket.socket(fileno=args.session_fd)
    asyncio.run(_serve(sock))


if __name__ == "__main__":
    main()
