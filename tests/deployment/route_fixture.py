"""Synthetic TLS peer and client for the emitted API route deployment.

The fixed signing key is deliberately public test material. The proxy receives
neither this fixture nor the TLS/signing inputs. Only the host orchestrator owns
the engine; these contained processes cannot manage containers.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import asyncio
import copy
import errno
import json
import os
import shutil
import socket
import time
import signal
import ssl
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


def key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(b"\x17" * 32)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True))
    temporary.replace(path)


async def peer(literal_ip):
    sys.path.insert(0, "/test-support")
    from tests.fakes.api_tls import api_tls_peer, verify_signed_request
    from tests.fakes.attempts import observation

    root = Path("/evidence")
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    async with api_tls_peer("success") as server:
        origin = server.origin.replace("127.0.0.1", "localhost")
        if literal_ip:
            origin = server.origin
        shutil.copyfile(Path(server.scratch.name) / "cert.pem", root / "ca.pem")
        write_json(root / "received.json", {"requests": 0, "server_names": []})

        async def respond(request, body):
            write_json(root / "received.json", {"requests": len(server.requests), "server_names": server.server_names})
            verify_signed_request(request, body, origin=origin, key=key())
            hosts = [line.split(b":", 1)[1].strip() for line in request.split(b"\r\n") if line.lower().startswith(b"host:")]
            if hosts != [origin.removeprefix("https://").encode()]:
                raise AssertionError("Proxy changed or duplicated the original HTTP authority")
            expected = b"GET /provider/v1/execution-attempts/execution_attempt:sha256:" + b"a" * 64 + b" HTTP/1.1"
            if request.split(b"\r\n", 1)[0] != expected:
                raise AssertionError("Synthetic peer received a different API target")
            return observation()

        server.response_factory = respond
        write_json(root / "peer.json", {"origin": origin})
        await stopping.wait()


def definition(origin):
    return f'''project = "route-proof"
uid = 65532
gid = 65532
request_seconds = 1
retry_initial_seconds = 0.01
retry_max_seconds = 0.1
[limits]
provider_items = 1
provider_bytes = 4096
total_items = 1
total_bytes = 4096
[[bindings]]
name = "fixture"
socket_path = "/run/delivery/delivery.sock"
[bindings.api]
origin = "{origin}"
provider_ref = "provider:route-proof"
profile = "dev-local"
topology = "dev-local"
credential_file = "/synthetic/unused-signing.json"
exchange_seconds = 1
retirement_seconds = 0.5
[bindings.route]
mode = "mapped"
interface = "lo"
log_level = "INFO"
addresses = ["{urlsplit(origin).hostname}=127.0.0.1"]
'''


async def peer_info():
    deadline = asyncio.get_running_loop().time() + 10
    path = Path("/evidence/peer.json")
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Synthetic TLS peer did not publish its owned readiness within ten seconds")
        await asyncio.sleep(0.05)
    print(definition(json.loads(path.read_text())["origin"]), end="")


def readonly_route(path):
    parent = path.parent
    facts = parent.stat()
    if (facts.st_uid, facts.st_gid, stat.S_IMODE(facts.st_mode)) != (65532, 65532, 0o700):
        raise AssertionError("Actual route root differs from its private provider identity grant")
    facts = path.stat()
    if not stat.S_ISSOCK(facts.st_mode) or (facts.st_uid, facts.st_gid, stat.S_IMODE(facts.st_mode)) != (65532, 65532, 0o600):
        raise AssertionError("Actual proxy socket differs from its owner-only birth grant")
    if (parent / "ready").read_bytes() != b"ready\n":
        raise AssertionError("Proxy readiness is not the exact owned receipt")
    operations = ((parent / "borrower-write").touch, path.unlink, lambda: path.chmod(0o600))
    for operation in operations:
        try:
            operation()
        except OSError as failure:
            if failure.errno != errno.EROFS:
                raise
        else:
            raise AssertionError("Read-only route borrower unexpectedly changed the mounted route")


async def client(mode, path):
    from provider_core.api import ApiFailure, ProviderAPI
    from provider_core.signing import SigningIdentity
    from provider_core.transport import HttpTransport

    if mode == "forbidden":
        try:
            path.stat()
        except PermissionError:
            print("Different UID cannot traverse the private route")
            return
        raise AssertionError("Different UID unexpectedly traversed the private route")
    readonly_route(path)
    origin = json.loads(Path("/evidence/peer.json").read_text())["origin"]
    socket = str(path)
    if mode == "direct":
        socket = None
    transport = HttpTransport(trust=ssl.create_default_context(cafile="/evidence/ca.pem"),
                              exchange_seconds=1, retirement_seconds=0.5, proxy_socket=socket)
    api = ProviderAPI(SigningIdentity(origin, "credential:test", key()), transport, expected_topology="dev-local")
    try:
        try:
            result = await api.read_attempt("execution_attempt:sha256:" + "a" * 64, job_ref="job:example")
        except ApiFailure:
            if mode != "lost":
                raise
        else:
            if mode == "lost":
                raise AssertionError("Stopped proxy request succeeded through an unintended direct route")
            if result.job_ref != "job:example" or result.state != "in_progress":
                raise AssertionError("Signed HTTPS reply did not pass the API contract")
    finally:
        await transport.retire()
    if not transport.retirement_confirmed:
        raise AssertionError("Original signed exchange did not settle before client exit")
    print(json.dumps({"mode": mode, "settled": True, "uid": os.geteuid()}))


def shared_group_socket(mode, path, owner_uid, access_gid):
    if os.geteuid() in (0, owner_uid) or os.getegid() == access_gid:
        raise AssertionError("Shared grant witness requires a distinct non-root UID and primary GID")
    if (access_gid in os.getgroups()) != (mode == "allowed"):
        raise AssertionError("Shared grant witness has incorrect supplementary groups")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    if not any(line.split()[4] == str(path.parent) and "ro" in line.split()[5].split(",") for line in mounts):
        raise AssertionError("Shared proxy grant must be mounted read-only")
    deadline = time.monotonic() + 10
    while True:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(3)
            try:
                channel.connect(str(path))
            except PermissionError as failure:
                if mode != "denied" or failure.errno != errno.EACCES:
                    raise
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if mode != "allowed" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
                continue
            if mode != "allowed":
                raise AssertionError("A nonmember connected to the shared proxy socket")
            channel.sendall(b"CONNECT witness.invalid:1 HTTP/1.1\r\n\r\n")
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 4096:
                chunk = channel.recv(4096 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
            if not response.startswith(b"HTTP/1.1 403 "):
                raise AssertionError("Shared proxy did not refuse the disallowed CONNECT")
            try:
                path.unlink()
            except OSError as failure:
                if failure.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
                    raise
            else:
                raise AssertionError("A read-only borrower removed the shared proxy socket")
            break
    print(f"Shared proxy socket access {mode}")


def compose(projection, image, namespace, operation, fixture, support):
    plan = projection["compose"]
    plan["name"] = namespace
    for service in plan["services"].values():
        service["labels"]["org.nmr-provider-tools.operation"] = operation
    plan["services"]["client"] = {
        "image": image, "pull_policy": "never", "profiles": ["proof-client"],
        "user": "65532:65532", "read_only": True, "init": True,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "network_mode": "none", "cpus": 2, "mem_limit": "256m", "memswap_limit": "256m", "pids_limit": 64,
        "entrypoint": ["/opt/provider-core/environment/bin/python", "-I", "-B", "/fixture.py", "client"],
        "command": ["success", projection["client_socket"]],
        "labels": {"org.nmr-provider-tools.operation": operation},
        "depends_on": projection["client_depends_on"],
        "volumes": [projection["client_mount"],
                    {"type": "volume", "source": "evidence", "target": "/evidence", "read_only": True, "volume": {"nocopy": True}},
                    {"type": "bind", "source": fixture, "target": "/fixture.py", "read_only": True, "bind": {"create_host_path": False}},
                    {"type": "bind", "source": support, "target": "/test-support/tests", "read_only": True, "bind": {"create_host_path": False}}],
        "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=32m,mode=0700,uid=65532,gid=65532"],
    }
    # The explicit canary can reach the TLS peer directly. Losing the selected
    # proxy must still fail, so a hidden transport fallback cannot pass.
    plan["services"]["canary"] = copy.deepcopy(plan["services"]["client"])
    plan["services"]["canary"]["network_mode"] = "host"
    for volume in plan["volumes"].values():
        volume["labels"] = {"org.nmr-provider-tools.operation": operation}
    plan["volumes"]["evidence"] = {"external": True, "name": namespace + "-evidence"}
    print(json.dumps(plan, ensure_ascii=True, sort_keys=True).replace("$", "$$"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("peer", "peer-info", "received"):
        command = commands.add_parser(name)
        if name == "peer":
            command.add_argument("--literal-ip", action="store_true")
    selected = commands.add_parser("client")
    selected.add_argument("mode", choices=("success", "lost", "direct", "forbidden"))
    selected.add_argument("socket", type=Path)
    shared = commands.add_parser("shared-group")
    shared.add_argument("mode", choices=("allowed", "denied"))
    shared.add_argument("socket", type=Path)
    shared.add_argument("owner_uid", type=int)
    shared.add_argument("access_gid", type=int)
    render = commands.add_parser("compose")
    render.add_argument("projection", type=Path)
    for name in ("image", "namespace", "operation", "fixture", "support"):
        render.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.action == "peer":
        asyncio.run(peer(args.literal_ip))
    elif args.action == "peer-info":
        asyncio.run(peer_info())
    elif args.action == "client":
        asyncio.run(client(args.mode, args.socket))
    elif args.action == "shared-group":
        shared_group_socket(args.mode, args.socket, args.owner_uid, args.access_gid)
    elif args.action == "received":
        print(Path("/evidence/received.json").read_text())
    else:
        compose(json.loads(args.projection.read_text()), args.image, args.namespace, args.operation, args.fixture, args.support)


if __name__ == "__main__":
    main()
