"""Qualify the core retirement seam with one real, non-scientific child.

Rationale: NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/runner_session.py:395-443 explicitly retires a channel without
claiming exit. SECS 34f5915c1534278bbf61b4c6592afa44cf94ab08,
src/secs_inference/provider/worker.py:251-295 reserves stop acknowledgement for
the supervisor that confirms its child exited. This fixture exercises that
distinction through AnalysisSession; it copies neither sibling protocol nor
scientific validation. Real process trees, grants and warm reuse stay unqualified.
"""

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path

from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence


class ChildChannelClosed(RuntimeError):
    """EOF prevented a reply; the retained child may still be executing."""


class ChildStopUnconfirmed(RuntimeError):
    """The owner has not confirmed exit and pipe cleanup for its exact child."""


class ScriptedChild:
    def __init__(self):
        self.process = None
        self.pipes_closed = False

    # silent-defaults: allow-contract -- Ordinary child tests lend no descriptors; the Upload lifetime witness explicitly lends its source directory.
    async def start(self, mode, *, pass_fds=()):
        if self.process is not None:
            raise RuntimeError("Scripted child already exists; retain and stop it instead of replacing it")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("child_peer.py")),
            mode,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
            pass_fds=pass_fds,
        )

    async def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        await self.process.stdin.drain()

    async def receive(self):
        line = await self.process.stdout.readline()
        if not line:
            raise ChildChannelClosed("Scripted child closed its result channel; confirm that same process stopped before reuse")
        return json.loads(line)

    async def stop(self):
        if self.process is None or self.pipes_closed:
            return
        if self.process.returncode is None:
            # A raced kill cannot attest exit. Still await the retained child
            # and drain its owned pipes below; no replacement process exists.
            with suppress(ProcessLookupError):
                self.process.kill()
        await asyncio.wait_for(self.process.wait(), 1)
        await asyncio.wait_for(self.process.communicate(), 1)
        await asyncio.wait_for(self.process.stdin.wait_closed(), 1)
        self.pipes_closed = True


class ChildSession:
    """A scripted admission/one-generation binding, with exact-child ownership."""

    def __init__(self, store, mode):
        self.store, self.mode = store, mode
        self.child = ScriptedChild()
        self.phases = []
        self.generations = 0
        self.generation_started = asyncio.Event()
        self._prepared = None

    # Match the required session port; child retirement tests emit no conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        if acknowledged.start.input != instructions.identity:
            raise ValueError("Scripted preparation requires the exact admitted input")
        self.phases.append(type((await self.store.inventory())[0]))
        await self.child.start(self.mode)
        fingerprint = instructions.identity.input_fingerprint
        await self.child.send({"operation": "prepare", "source_fingerprint": fingerprint})
        reply = await self.child.receive()
        evidence = ProtectedEvidence("fixture.child.evidence.v1", b"scripted preparation, not chemical validation")
        if reply == {"state": "rejected"}:
            return AnalysisFailure("input_rejected", "The scripted peer rejected preparation before generation.", evidence)
        if reply != {"state": "prepared", "source_fingerprint": fingerprint}:
            raise ValueError("Scripted preparation did not acknowledge the exact source")
        self._prepared = PreparedAnalysis(ProtectedEvidence("fixture.child.input.v1", fingerprint.encode("ascii")))
        return self._prepared

    # Match the required session port; child retirement tests emit no conditions.
    async def execute(self, handle, *, report_condition):  # noqa: ARG002
        if handle is not self._prepared or handle is None:
            raise ValueError("Scripted generation requires this session's current unused handle")
        self._prepared = None
        self.phases.append(type((await self.store.inventory())[0]))
        self.generations += 1
        await self.child.send({"operation": "generate"})
        if await self.child.receive() != {"state": "generating"}:
            raise ValueError("Scripted child did not acknowledge generation")
        self.generation_started.set()
        result = await self.child.receive()
        return AnalysisSuccess(
            "fixture.child.result.v1",
            json.dumps(result).encode(),
            ProtectedEvidence("fixture.child.evidence.v1", b"one scripted generation"),
        )

    async def retire(self):
        self._prepared = None
        if self.mode == "close_output" and not self.child.pipes_closed:
            raise ChildStopUnconfirmed(
                "Scripted child stop is unconfirmed; retain its exact process and explicitly settle after cleanup"
            )
        await self.child.stop()
