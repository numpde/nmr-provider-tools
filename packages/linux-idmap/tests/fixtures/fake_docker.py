#!/usr/bin/env python3
"""A tiny persistent engine model, deliberately separate from client replies.

This is not a Docker emulator or real-engine qualification. It exercises the
actual subprocess transport and /proc reader under interrupted client replies.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

STATE = Path(os.environ["LINUX_IDMAP_TEST_STATE"])
CID = "c" * 64
IMAGE = "sha256:" + "a" * 64


def save(state):
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(STATE)


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


args = sys.argv[1:]
state = json.loads(STATE.read_text())
endpoint = option(args, "--host")
while args and args[0] in ("--host", "--config"):
    args = args[2:]
kind = args[1] if args[0] in ("image", "container") else args[0]
# silent-defaults: allow-contract -- Initialize the persistent fixture command log on its first invocation.
state.setdefault("calls", []).append({"kind": kind, "args": args, "endpoint": endpoint})
# silent-defaults: allow-contract -- Initialize the persistent fixture client inventory on its first invocation.
state.setdefault("client_pids", []).append(os.getpid())
result = ""
status = 0

if args[0] == "info":
    # silent-defaults: allow-contract -- The fixture daemon identity is stable until an engine-change fault explicitly replaces it.
    result = json.dumps([state.get("daemon", "fixture-engine"), [], "/fixture/docker", "overlay2", "linux"])
elif args[:2] == ["image", "inspect"]:
    result = json.dumps([IMAGE, None])
elif args[:2] == ["container", "create"]:
    labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
    image_index = args.index(IMAGE)
    state["container"] = {
        "cid": CID, "name": "/" + option(args, "--name"), "image_id": IMAGE,
        "token": labels["linux-idmap.token"],
        "owner": labels["linux-idmap.operation"], "pid": 0,
        "running": False, "started_at": "",
        "user": option(args, "--user"),
        # silent-defaults: allow-contract -- The model encodes explicit host namespace selection as Docker does; otherwise inspect returns the default empty string.
        "userns_mode": "host" if "--userns=host" in args else "",
        "entrypoint": [option(args, "--entrypoint")], "command": args[image_index + 1:],
        "mounts": [],
        "isolation": {
            "AutoRemove": "--rm" in args, "NetworkMode": option(args, "--network"),
            "ReadonlyRootfs": "--read-only" in args, "CapDrop": [option(args, "--cap-drop")],
            "SecurityOpt": [option(args, "--security-opt")],
            "Privileged": False, "PidMode": "", "CapAdd": None,
            "Devices": [], "DeviceRequests": None,
            "RestartPolicy": {"Name": option(args, "--restart", "no"), "MaximumRetryCount": 0},
        },
    }
    if state.get("foreign_token"):
        state["container"]["token"] = "f" * 32
    if state.get("change_engine_after_create"):
        state["daemon"] = "different-engine"
    result = CID
elif args[:2] == ["container", "start"]:
    container = state["container"]
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(90)", container["token"]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # silent-defaults: allow-contract -- Initialize the persistent fixture probe inventory only when the first modeled start creates a process.
    state.setdefault("probe_processes", []).append({"pid": process.pid, "token": container["token"]})
    container.update(pid=process.pid, running=True, started_at="fixture-start")
    result = CID
elif args[:2] == ["container", "ls"]:
    container = state.get("container")
    selector = option(args, "--filter", "")
    if container and not state.get("hide_container"):
        if selector == "id=" + CID or selector == "label=linux-idmap.token=" + container["token"]:
            result = CID
elif args[:2] == ["container", "inspect"]:
    container = state.get("container")
    if container:
        evidence = dict(container)
        fields = re.findall(r'"([A-Za-z]+)":{{json \.HostConfig\.', option(args, "--format", ""))
        evidence["isolation"] = {key: value for key, value in container["isolation"].items() if key in fields}
        result = json.dumps(evidence)
    else:
        status = 1
elif args[:2] == ["container", "rm"]:
    container = state.get("container")
    if state.get("deny_remove"):
        status = 1
    elif container and args[-1] == container["cid"]:
        if container["pid"] and not state.get("leave_process_alive"):
            try:
                os.kill(container["pid"], signal.SIGTERM)
            # silent-defaults: allow-contract -- The modeled probe may already have exited; removal of its container still succeeds.
            except ProcessLookupError:
                pass
        state["container"] = None
    else:
        status = 1
else:
    status = 1

hang = state.get("hang_after") == kind
if hang:
    state["hang_after"] = None
save(state)
if hang:
    # The mutation is visible before the real CLI subprocess is timed out/killed.
    time.sleep(20)
if status:
    print("SECRET engine diagnostics must not escape", file=sys.stderr)
else:
    # silent-defaults: allow-contract -- Docker fixture replies have a trailing newline only when a nonempty reply exists.
    sys.stdout.write(result + ("\n" if result else ""))
sys.exit(status)
