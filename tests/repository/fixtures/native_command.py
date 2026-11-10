#!/usr/bin/env python3
"""Executable native-command fault port; never silently use the real utility."""

from tools.container_guard import require_container

require_container()

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def fixture_command(name):
    """Inject only named local file faults; all other operations use native tools."""
    arguments = sys.argv[1:]
    if name == "mv":
        phase = os.environ["NATIVE_PUBLICATION_FAILURE"]
        source, target = map(Path, arguments[-2:])
        receipt = target.parent / "status.ready"
        if target.name == "status.ready":
            anchor = int(source.read_text())
        elif receipt.exists():
            anchor = int(receipt.read_text())
        else:
            anchor = 0
        Path(os.environ["NATIVE_CONTROL"]).write_text(json.dumps({"anchor": anchor, "directory": str(target.parent)}))
        if target.name == "status.ready" and os.environ.get("NATIVE_CORRUPT_READY") == "1":
            source.write_bytes(b"not-the-anchor\n")
        if target.name == "status.release" and phase.startswith("release-"):
            if phase == "release-fifo":
                os.mkfifo(target)
            elif phase == "release-directory":
                target.mkdir()
            else:
                target.symlink_to(source)
            return 0
        if target.name == "status." + phase or (target.name == "status" and phase == "status"):
            if os.environ.get("NATIVE_CANCEL_PUBLICATION") == "1":
                parent = int(Path("/proc", str(anchor), "stat").read_text().split()[3])
                os.kill(parent, signal.SIGTERM)
            if "NATIVE_PUBLICATION_FAILED_AT" in os.environ:
                Path(os.environ["NATIVE_PUBLICATION_FAILED_AT"]).write_text(str(time.monotonic()))
            return 29
        if target.name == "status":
            mutation = os.environ["NATIVE_STATUS_MUTATION"]
            values = {
                "nul": b"0\0\n",
                "cr": b"0\r\n",
                "missing-lf": b"0",
                "extra-lf": b"0\n\n",
                "extra-line": b"0\n7\n",
                "negative": b"-1\n",
                "256": b"256\n",
                "text": b"unknown\n",
            }
            if mutation in values:
                source.write_bytes(values[mutation])
            if mutation == "symlink":
                target.symlink_to(source)
                return 0
            if mutation in {"fifo", "directory", "deadline-fifo", "deadline-directory", "deadline-symlink"}:
                if mutation.startswith("deadline-"):
                    target = target.parent / "status.deadline"
                if mutation.endswith("fifo"):
                    os.mkfifo(target)
                elif mutation.endswith("directory"):
                    target.mkdir()
                else:
                    target.symlink_to(source)
                return 0
    if name == "head" and arguments[-1].endswith("/status") and os.environ["NATIVE_STATUS_MUTATION"] == "read-error":
        return 31
    if name == "head" and arguments == ["-c", "8388608"] and os.environ.get("NATIVE_DRAIN_FAILURE") == "1":
        return 33
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "1":
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        return 23
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
        # Actual deletion plus a secondary failure must not invent retained files
        # or block the stdio owner by writing another message to its native pipe.
        subprocess.run(["/bin/rm", *arguments], check=True)
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        sys.stderr.write("fixture cleanup failed after deletion\n")
        return 23
    os.execv("/bin/" + name, [name, *arguments])
    return 97


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    if name not in {"mv", "head", "rm"}:
        raise SystemExit("Use a named test utility symlink: mv, head or rm.")
    raise SystemExit(fixture_command(name))
