"""Join acquired-file consumption to the existing test-owned child session.

SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/worker.py::WorkerStopUnconfirmed/WorkerClient.stop
and runtime.py::run_execution retain source files when child exit is unconfirmed.
This proposed binding exercises that contract against installed core using the
existing scripted child, not the sibling's supervisor or scientific protocol.
The test composition exclusively owns the source leaf until confirmed cleanup;
concurrent replacement, process trees, warm reuse and capacity are outside scope.
"""

import asyncio
import json
import os
from dataclasses import asdict

from provider_core.analysis import PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence
from tests.fakes.child_session import ChildSession


class UploadChildSession(ChildSession):
    """Keep an acquired leaf and borrowed directory alive through child retirement."""

    def __init__(self, store, mode, source, *, directory_fd):
        super().__init__(store, mode)
        self.source, self.directory_fd = source, directory_fd
        self.source_reply = None
        self.source_read_reply = None
        self.reading_started = asyncio.Event()
        self._launch = None
        self.source_removals = 0
        self.source_cleanup_observed_stopped_child = False

    # Match the core session port; this lifetime fixture emits no conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        if acknowledged.start.input != instructions.identity or self.source.job_ref != instructions.job_ref:
            raise ValueError("Upload child preparation requires this Job's admitted instructions and source receipt")
        self.phases.append(type((await self.store.inventory())[0]))
        self._launch = asyncio.create_task(self.child.start(self.mode, pass_fds=(self.directory_fd,)))
        await asyncio.shield(self._launch)
        await self.child.send(
            {
                "operation": "prepare",
                "source_fingerprint": instructions.identity.input_fingerprint,
                "upload": asdict(self.source),
                "directory_fd": self.directory_fd,
            }
        )
        reply = await self.child.receive()
        if reply.get("state") == "reading":
            self.source_read_reply = reply
            self.reading_started.set()
            reply = await self.child.receive()
        if (
            reply.get("state") != "prepared"
            or reply.get("source_fingerprint") != instructions.identity.input_fingerprint
            or reply.get("content_hash") != self.source.content_hash
        ):
            raise ValueError("Upload child did not acknowledge its exact admitted input and verified source")
        self.source_reply = reply
        identity = json.dumps({"upload": asdict(self.source), "input": reply}, sort_keys=True).encode()
        self._prepared = PreparedAnalysis(ProtectedEvidence("fixture.upload_child.input.v1", identity))
        return self._prepared

    async def stop_child(self):
        """Test teardown must join a pending launch before releasing its lent fd."""
        if self._launch is not None:
            await asyncio.shield(self._launch)
        await self.child.stop()

    async def retire(self):
        if self._launch is not None:
            # Cancellation cannot hide a later-created child. Join the original
            # launch before borrowing its exact process-exit confirmation.
            await asyncio.shield(self._launch)
            await super().retire()
        if self.source_removals == 0:
            if self._launch is not None:
                self.source_cleanup_observed_stopped_child = self.child.process.returncode is not None and self.child.pipes_closed
                if not self.source_cleanup_observed_stopped_child:
                    raise AssertionError("Test binding attempted source cleanup before its exact child stopped")
            os.unlink(self.source.name, dir_fd=self.directory_fd)
            self.source_removals += 1
