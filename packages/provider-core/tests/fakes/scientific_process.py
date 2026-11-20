"""A witnessed contribution for the installed process host's lifecycle proofs.

The test owns the witness directory and release files. Only the lost-reply
scenario intercepts a worker handoff: real science finishes first, then its
original admission acknowledgement waits for the independent parent witness.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import socket
import sys
import time

import provider_core
from provider_core.hosting import observe_completion
from provider_core.scientific import AssetAccess, Contribution, operation


def record_event(root: Path, event: str, **facts) -> None:
    encoded = (json.dumps({"event": event, "pid": os.getpid(), **facts}) + "\n").encode()

    if not (len(encoded) <= 4096):
        raise ValueError("Scientific fixture witness exceeds its event allowance")

    with (root / "events.jsonl").open(mode="ab", buffering=0) as witness:
        written = witness.write(encoded)

    if not (written == len(encoded)):
        raise OSError("Scientific fixture witness was not written completely")


def witness_events(root: Path) -> list[dict]:
    try:
        encoded = (root / "events.jsonl").read_bytes()
    except FileNotFoundError:
        return []

    # A reader may run during one append. Only complete records are evidence.
    return [json.loads(line) for line in encoded.splitlines(keepends=True) if line.endswith(b"\n")]


def wait_for_event(root: Path, event: str) -> dict:
    deadline = time.monotonic() + 10

    while True:
        for observed in witness_events(root):
            if observed["event"] == event:
                return observed

        if not (time.monotonic() < deadline):
            raise TimeoutError(f"Scientific fixture did not record {event=}")

        time.sleep(0.01)


def wait_for_release(release: Path) -> None:
    deadline = time.monotonic() + 10

    while not release.exists():
        if not (time.monotonic() < deadline):
            raise TimeoutError(f"Scientific fixture barrier remains held: {release=}")

        time.sleep(0.01)


@dataclass(frozen=True, slots=True, kw_only=True)
class Number:
    value: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Calculation:
    value: int
    calls: int
    process_id: int


class WitnessedScience(Contribution):
    def __init__(self, *, startup):
        super().__init__()

        self.root = Path(startup["witness_directory"])
        self.scenario = startup["scenario"]
        self.unrelated_identity = tuple(startup["unrelated_identity"])
        self.resource = None
        self.calls = 0

        record_event(self.root, "constructed")

    def initialize(self, *, assets: AssetAccess) -> None:
        self.resource = (self.root / "partial-resource").open(mode="xb")

        inherited_unrelated = False

        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                observed = os.fstat(int(descriptor.name))
            except OSError as cause:
                if not (cause.errno == errno.EBADF):
                    raise
            else:
                inherited_unrelated |= (observed.st_dev, observed.st_ino) == self.unrelated_identity

        record_event(
            root=self.root,
            event="initialization_acquired",
            allowed_environment=os.environ["SCIENTIFIC_PROCESS_ALLOWED"],
            unrelated_environment="SCIENTIFIC_PROCESS_UNRELATED" in os.environ,
            unrelated_descriptor=inherited_unrelated,
            sdk_path=str(Path(provider_core.__file__).resolve()),
            installation=str(Path(sys.prefix).resolve()),
        )

        if self.scenario == "cancel-start":
            wait_for_release(self.root / "release-initialization")

        if not (self.scenario != "independent-failures"):
            raise OSError("Witnessed partial model initialization failed")

        record_event(self.root, "initialized")

    @operation()
    def calculate(self, *, number: Number) -> Calculation:
        self.calls += 1

        record_event(self.root, "calculation_started", calls=self.calls)

        if self.scenario == "active-kill":
            # No timer may finish science between its active witness and the
            # parent's kill. Test teardown releases this gate after an assertion
            # failure; the normal proof ends it by terminating the worker.
            while not (self.root / "release-calculation").exists():
                time.sleep(0.01)

        (self.root / "effect").write_text(str(self.calls), encoding="utf-8")
        record_event(self.root, "effect", calls=self.calls)

        return Calculation(value=number.value + self.calls, calls=self.calls, process_id=os.getpid())

    def close(self) -> None:
        record_event(self.root, "close_entered")

        if self.resource is not None:
            self.resource.close()
            (self.root / "partial-resource").unlink()

        if not (self.scenario != "independent-failures"):
            raise OSError("Witnessed partial resource close failed")

        record_event(self.root, "closed")


def construct_science(*, startup) -> WitnessedScience:
    return WitnessedScience(startup=startup)


def run_worker() -> None:
    """Run installed worker code, with one explicit acknowledgement-loss barrier."""
    from provider_core import process_worker

    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument("--session-fd", required=True, type=int)

    (arguments, _) = parser.parse_known_args()
    original_command = process_worker._Worker.command
    witness_root = None
    lose_reply = False

    async def witnessed_command(worker, message):
        nonlocal witness_root, lose_reply

        if message["action"] == "start":
            witness_root = Path(message["startup"]["witness_directory"])
            lose_reply = message["startup"]["scenario"] == "lost-reply"

        if message["action"] == "submit":
            record_event(witness_root, "submit_received", operation_id=message["invocation"].operation_id)

        response = await original_command(worker, message)

        if (message["action"] == "submit") and lose_reply and ("operation" in response):
            operation_ref = response["operation"]
            original = worker.operations[(operation_ref.analysis, operation_ref.operation_id)]

            async with asyncio.timeout(10):
                await observe_completion(original)

            record_event(witness_root, "reply_waiting", operation_id=operation_ref.operation_id)
            await asyncio.to_thread(wait_for_release, witness_root / "release-reply")
            record_event(witness_root, "reply_dropped", operation_id=operation_ref.operation_id)

            with socket.socket(fileno=os.dup(arguments.session_fd)) as channel:
                channel.shutdown(socket.SHUT_RDWR)

        return response

    process_worker._Worker.command = witnessed_command

    process_worker.main()
