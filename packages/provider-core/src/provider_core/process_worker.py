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
from collections.abc import Mapping
import importlib
import os
import socket

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import (
    AdmissionCode,
    CapacityUnavailable,
    CleanupObservation,
    NotAdmitted,
    ResultState,
    settle_cleanup,
)
from provider_core.process_wire import RemoteScienceError, frame, receive_async
from provider_core.scientific import Contribution
from provider_core.type_requirements import require_subclass


def _installed_member(member_location: str):
    (module_name, separator, member_path) = member_location.partition(":")

    if not (separator and module_name and member_path and ("<locals>" not in member_path)):
        raise ValueError(f"Scientific worker {member_location=} should identify an installed module:member")

    installed_member = importlib.import_module(module_name)

    for name in member_path.split("."):
        installed_member = getattr(installed_member, name)

    return installed_member


class _Worker:
    def __init__(self):
        self.host = None
        self.hosted = None
        self.ready = False
        self.analysis = None
        self.operations = {}

    async def start(self, message: Mapping) -> dict:
        if not (self.host is None):
            raise RuntimeError("Scientific worker startup is single-use")

        host = InProcessExecutionHost(**message["host_options"])
        self.host = host

        await host.__aenter__()

        constructor = _installed_member(message["constructor"])
        contribution_type = _installed_member(message["contribution_type"])

        require_subclass(contribution_type, Contribution)

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

        self.ready = True

        return {
            "allocation": hosted.allocation,
            "binding_revision": hosted.description.binding_revision,
            "process_id": os.getpid(),
        }

    def _analysis(self, identity):
        if not ((self.analysis is not None) and (self.analysis.identity == identity)):
            raise LookupError(f"Scientific analysis {identity=} should identify the worker's open analysis")

        return self.analysis

    def _operation(self, message):
        key = (message["analysis"], message["operation"])
        operation = self.operations.get(key)

        if not (operation is not None):
            raise LookupError(f"Scientific operation {key=} should identify work retained by this worker")

        return operation

    async def _forget_analysis(self, analysis_id):
        """Release transferred evidence only after successful analysis cleanup."""

        analysis = self._analysis(analysis_id)

        if not (analysis.cleanup is not None):
            raise RuntimeError("Scientific analysis cleanup has not been requested")

        observed = await analysis.cleanup.observe(after=None, wait_seconds=0)

        if not (observed.settled and (not observed.failures)):
            raise RuntimeError(
                f"Scientific analysis {analysis_id=} cleanup should settle successfully; "
                f"{observed.settled=}, {len(observed.failures)=}"
            )

        for (key, operation) in list(self.operations.items()):
            if key[0] != analysis_id:
                continue

            current = await operation.observe(after=None, wait_seconds=0)

            if current.result_state is not ResultState.AVAILABLE:
                del self.operations[key]

        self.analysis = None

    async def command(self, message: Mapping) -> object:
        action = message["action"]

        if action == "start":
            return await self.start(message)

        # Retirement must remain available after partial startup, even if no
        # contribution was constructed or initialized.
        if action == "retire":
            return await self.retire()

        if not (self.ready):
            raise RuntimeError("Scientific worker is not initialized")

        if action == "open_analysis":
            if not (self.analysis is None):
                raise CapacityUnavailable("The scientific allocation already owns an analysis")

            self.analysis = self.host.open_analysis(
                analysis_id=message["analysis"],
                allocation_names=(self.hosted.allocation.allocation,),
            )

            return self.analysis.reservation(allocation=self.hosted.allocation)

        if action == "submit":
            invocation = message["invocation"]
            analysis_id = invocation.reservation.analysis
            analysis = self.analysis

            if (analysis is None) or (analysis.identity != analysis_id):
                return {
                    "refusal": NotAdmitted(
                        AdmissionCode.INVALID_REFERENCE,
                        "Scientific submission requires its explicitly opened analysis",
                    )
                }

            decision = analysis.submit(invocation=invocation)

            if isinstance(decision, NotAdmitted):
                return {"refusal": decision}

            self.operations[(analysis_id, invocation.operation_id)] = decision

            return {"operation": decision.reference}

        if action == "observe":
            return await self._operation(message).observe(after=None, wait_seconds=0)

        if action == "read_result":
            return await self._operation(message).read_result(result=message["result"])

        if action == "stop_operation":
            self._operation(message).request_stop()

            return None

        if action == "release_result":
            operation = self._operation(message)
            control = operation.release_result(result=message["result"])
            observed = await control.observe(after=None, wait_seconds=0)
            current = await operation.observe(after=None, wait_seconds=0)

            if (
                observed.settled and
                ((self.analysis is None) or (self.analysis.identity != message["analysis"]))
            ):
                del self.operations[(message["analysis"], message["operation"])]

            return {"cleanup": observed, "operation": current}

        if action == "stop_analysis":
            self._analysis(message["analysis"]).request_stop()

            return None

        if action == "close_analysis":
            control = self._analysis(message["analysis"]).close()

            return await control.observe(after=None, wait_seconds=0)

        if action == "forget_analysis":
            await self._forget_analysis(message["analysis"])

            return None

        raise ValueError(f"Scientific worker {action=} should name a supported command")

    async def retire(self):
        self.ready = False

        if self.host is None:
            return CleanupObservation(revision=0, settled=True, allowance_expired=False, failures=())

        return await settle_cleanup(control=self.host.retire())


async def _serve(science_socket: socket.socket) -> None:
    science_socket.setblocking(False)

    worker = _Worker()

    try:
        while True:
            try:
                message = await receive_async(science_socket)
            except EOFError:
                break

            try:
                command_result = await worker.command(message)
            except BaseException as cause:
                response_envelope = {"ok": False, "error": cause}
            else:
                response_envelope = {"ok": True, "value": command_result}

            try:
                response_bytes = frame(response_envelope)
            except Exception as cause:
                failure = RemoteScienceError(
                    "Scientific reply could not be encoded; retain the original work without repeating it"
                )

                failure.__cause__ = cause
                response_bytes = frame({"ok": False, "error": failure})

            # Framing has no I/O effects. A failed send may have delivered a
            # partial reply and is never retried on this stream.
            await asyncio.get_running_loop().sock_sendall(science_socket, response_bytes)

            if message["action"] == "retire":
                break
    finally:
        try:
            # Always join the original host owner, including after a failed
            # factory import or partial model construction.
            await worker.retire()
        finally:
            science_socket.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve one installed scientific allocation")

    parser.add_argument("--session-fd", required=True, type=int)
    parser.add_argument("--lease-fd", required=True, type=int)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)

    # The native runner passes its lifetime lease. Keep that descriptor open
    # until process exit, including while scientific executor threads unwind.
    os.fstat(args.lease_fd)

    science_socket = socket.socket(fileno=args.session_fd)

    asyncio.run(_serve(science_socket))


if __name__ == "__main__":
    main()
