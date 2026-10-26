"""Freeze and initialize shared delivery without importing a scientific provider.

Ports nmrnet-repro d09b77b nmrnet_provider/delivery_deployment.py and its Compose
primitives. The neutral definition owns API identity, trust, routing and socket
grants. This finite program never contacts an Engine or API.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w

from provider_core.api_config import api_settings
from provider_core.deployment_binding import BindingDefinitionRejected, load_deployment_bindings
from provider_core.diagnostics import failure_evidence
from provider_core.failure_projection import owned_details
from provider_core.startup_file import read_startup_file
from provider_core.transport import require_unix_socket_path

# Both daemon and generic proxies use the installed, pip-free environment.
# repo-check: allow-module-constant
CORE_PYTHON = "/opt/provider-core/environment/bin/python"


class DeliveryDeploymentRejected(ValueError):
    """A code-owned grant refusal; arbitrary borrowed input is never its text."""


def _details(error, text, diagnostic_window):
    if type(error) in (DeliveryDeploymentRejected, BindingDefinitionRejected):
        return {"message": str(error)}
    return owned_details(error)


def _path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(c in str(path) for c in ("\0", "\n", "\r", ",")):
        raise DeliveryDeploymentRejected("Delivery inputs require canonical absolute paths without mount delimiters.")
    return path


def _write(path, content):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# silent-defaults: allow-contract -- Read-only is the grant for frozen inputs; only explicit runtime leaves opt into writing.
def _bind(source, target, *, readonly=True):
    return {"type": "bind", "source": str(source), "target": str(target), "read_only": readonly,
            "bind": {"create_host_path": False}}


def _volume(name, target):
    return {"type": "volume", "source": name, "target": target, "volume": {"nocopy": True}}


def _service(image, module, definition, labels, stop_seconds):
    uid, gid = definition.uid, definition.gid
    return {
        "image": image, "pull_policy": "never", "entrypoint": [CORE_PYTHON, "-I", "-B", "-m", module],
        "user": f"{uid}:{gid}", "init": True, "read_only": True, "cap_drop": ["ALL"],
        "ipc": "private", "cgroup": "private", "security_opt": ["no-new-privileges:true"],
        "cpus": 2, "mem_limit": "512m", "memswap_limit": "512m", "pids_limit": 64,
        "restart": "unless-stopped", "stop_grace_period": f"{stop_seconds}s",
        "network_mode": "none", "labels": dict(labels),
        "tmpfs": [f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}" for name in ("tmp", "run")],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3",
                    "mode": "non-blocking", "max-buffer-size": "1m"}},
    }


def _proxy(binding, definition, image, key, output):
    route = binding.route
    leaf = output / "runtime" / key
    leaf.mkdir(mode=0o700)
    service = _service(image, "provider_core.provider_proxy", definition,
                       {"org.nmr-provider-tools.delivery": definition.project,
                        "org.nmr-provider-tools.binding": key}, 15)
    parsed = urlsplit(binding.api["origin"])
    port = parsed.port
    if port is None:
        port = 443
    command = ["--mode", route["mode"], "--interface", route["interface"], "--log-level", route["log_level"],
               "--socket", "/route/proxy.sock", "--ready-file", "/route/ready", "--status-directory", "/run",
               "--authority", f"{parsed.hostname}:{port}"]
    service["volumes"] = [_bind(leaf, "/route", readonly=False)]
    networks = {}
    if route["mode"] == "local":
        network = "api-" + key[:16]
        del service["network_mode"]
        service["networks"] = [network]
        networks[network] = {"external": True, "name": route["network_id"]}
    else:
        service["network_mode"] = "host"
    if route["mode"] == "external":
        resolver = output / "bindings" / key / "resolv.conf"
        _write(resolver, _read_grant(route["resolver_file"], maximum_bytes=65536, label="API resolver"))
        service["volumes"].append(_bind(resolver, "/etc/resolv.conf"))
    else:
        for address in route["addresses"]:
            command.extend(("--address", address))
    service["command"] = command
    service["healthcheck"] = {"test": ["CMD", CORE_PYTHON, "-I", "-B", "-m", "provider_core.process_status",
                                         "/run/status.json", "proxy"],
                              "interval": "5s", "timeout": "2s", "retries": 3, "start_period": "5s"}
    return service, networks, leaf


def _read_grant(value, **limits):
    # Host grants mirror selected absolute filenames below /grants. A credential
    # path can therefore never mask image software or helper control inputs.
    path = Path("/grants") / Path(value).relative_to("/")
    return read_startup_file(path, **limits)


def _freeze_api(binding, directory, key):
    consumed = {}

    def read_input(value, **limits):
        path = Path(value)
        if path not in consumed:
            consumed[path] = _read_grant(path, **limits)
        if len(consumed[path]) > limits["maximum_bytes"]:
            raise DeliveryDeploymentRejected("A repeated API trust input exceeds its admitted byte bound.")
        return consumed[path]

    # Admit credential/provider/profile and TLS trust before issuing runtime grants.
    api_settings(binding.api, read_input)
    api = dict(binding.api)
    _write(directory / "signing.json", consumed[Path(api["credential_file"])])
    api["credential_file"] = f"/input/bindings/{key}/signing.json"
    if "ca_file" in api:
        _write(directory / "ca.pem", consumed[Path(api["ca_file"])])
        api["ca_file"] = f"/input/bindings/{key}/ca.pem"
    return api


def admit_listeners(definition):
    listeners = {}
    for binding in definition.bindings:
        key = binding.ipc_volume.removeprefix(definition.project + "-ipc-")
        listener = f"/ipc/{key}/{binding.socket_path.name}"
        try:
            require_unix_socket_path(listener)
        except ValueError as cause:
            raise DeliveryDeploymentRejected("Cannot grant delivery socket: the derived daemon path must fit Linux's 107-byte limit; shorten the provider socket filename.") from cause
        listeners[binding.name] = key, listener
    return listeners


def prepare(definition_path, output, image):
    definition = load_deployment_bindings(definition_path)
    if definition.uid != os.geteuid() or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise DeliveryDeploymentRejected("Delivery freezing requires its installing nonzero UID and an immutable image ID.")
    output = _path(output)
    facts = output.lstat()
    if (not stat.S_ISDIR(facts.st_mode) or facts.st_uid != definition.uid
            or stat.S_IMODE(facts.st_mode) != 0o700 or any(output.iterdir())):
        raise DeliveryDeploymentRejected("Delivery output requires a precreated empty caller-owned mode 0700 directory.")
    # Consumer paths can fit while this longer daemon pathname does not. Refuse
    # the complete definition before creating outputs or opening any API secret.
    listeners = admit_listeners(definition)
    (output / "bindings").mkdir(mode=0o700)
    (output / "runtime").mkdir(mode=0o700)
    queue = definition.project + "-queue"
    services, networks, volumes = {}, {}, {queue: {"name": queue}}
    daemon = _service(image, "provider_core.delivery_main", definition,
                      {"org.nmr-provider-tools.delivery": definition.project}, 60)
    daemon["command"] = ["--config", "/input/daemon.toml"]
    daemon["depends_on"] = {"init": {"condition": "service_completed_successfully"}}
    daemon["volumes"] = [_bind(output / "daemon.toml", "/input/daemon.toml"), _volume(queue, "/state")]
    configuration = {"schema_id": "provider-core.delivery.v1", "state_directory": "/state",
                     "request_seconds": definition.request_seconds, "retry_initial_seconds": definition.retry_initial_seconds,
                     "retry_max_seconds": definition.retry_max_seconds, "limits": definition.limits, "bindings": []}
    roots = [(queue, "/volumes/queue")]
    for binding in definition.bindings:
        key, listener = listeners[binding.name]
        directory = output / "bindings" / key
        directory.mkdir(mode=0o700)
        api = _freeze_api(binding, directory, key)
        proxy, attached, leaf = _proxy(binding, definition, image, key, output)
        role = "api-" + key[:16]
        if role in services:
            raise DeliveryDeploymentRejected("Delivery proxy identities collide; no ambiguous recipe was published.")
        services[role] = proxy
        networks.update(attached)
        volumes[binding.ipc_volume] = {"name": binding.ipc_volume}
        roots.append((binding.ipc_volume, "/volumes/ipc-" + key))
        daemon["volumes"].extend((_bind(directory / "signing.json", f"/input/bindings/{key}/signing.json"),
                                  _volume(binding.ipc_volume, "/ipc/" + key), _bind(leaf, "/routes/" + key)))
        if "ca_file" in api:
            daemon["volumes"].append(_bind(directory / "ca.pem", f"/input/bindings/{key}/ca.pem"))
        configuration["bindings"].append({"socket_path": listener, "socket_group_id": definition.gid,
                                           "proxy_socket": f"/routes/{key}/proxy.sock", "api": api})
        daemon["depends_on"][role] = {"condition": "service_healthy"}
    initializer = _service(image, "provider_core.delivery_main", definition,
                           {"org.nmr-provider-tools.delivery": definition.project}, 10)
    initializer.update(user="0:0", restart="no", cap_add=["CHOWN"], network_mode="none",
                       entrypoint=[CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"],
                       command=["initialize", "--uid", str(definition.uid), "--gid", str(definition.gid), "--queue", "/volumes/queue"],
                       volumes=[_volume(volume, target) for volume, target in roots])
    for volume, target in roots[1:]:
        initializer["command"].extend(("--ipc", target))
    services.update(delivery=daemon, init=initializer)
    _write(output / "daemon.toml", tomli_w.dumps(configuration).encode())
    for service in services.values():
        service["labels"]["org.nmr-provider-tools.root"] = str(output.parent.parent)
    plan = {"name": definition.project, "services": services, "networks": networks, "volumes": volumes}
    _write(output / "compose.json", (json.dumps(plan, sort_keys=True, indent=2).replace("$", "$$") + "\n").encode())
    for directory in (output / "bindings", *(output / "bindings").iterdir(), output / "runtime", output):
        _sync(directory)


def initialize(uid, gid, queue, ipc):
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise DeliveryDeploymentRejected("Volume initialization requires root with CHOWN and explicit nonzero target UID/GID.")
    for target, mode in [(queue, 0o700), *((path, 0o710) for path in ipc)]:
        path = _path(target)
        observed = path.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            raise DeliveryDeploymentRejected("Delivery volume root must be a real directory.")
        if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, mode):
            continue
        if observed.st_uid != 0 or any(path.iterdir()):
            raise DeliveryDeploymentRejected("Refusing to change an existing delivery volume with unexpected ownership, mode or contents.")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--definition", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--image", required=True)
    inspect = commands.add_parser("inspect-definition")
    inspect.add_argument("--definition", type=Path, required=True)
    initializer = commands.add_parser("initialize")
    for name in ("uid", "gid"):
        initializer.add_argument("--" + name, type=int, required=True)
    initializer.add_argument("--queue", type=Path, required=True)
    initializer.add_argument("--ipc", type=Path, action="append", required=True)
    options = parser.parse_args()
    if options.command == "prepare":
        prepare(options.definition, options.output, options.image)
    elif options.command == "inspect-definition":
        definition = load_deployment_bindings(options.definition)
        admit_listeners(definition)
        for value in (definition.project, str(definition.uid), str(definition.gid)):
            sys.stdout.buffer.write(value.encode() + b"\0")
    else:
        initialize(options.uid, options.gid, options.queue, options.ipc)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:
        sys.stderr.write(json.dumps(failure_evidence(error, secrets=(), describe_owner=_details), separators=(",", ":")) + "\n")
        raise SystemExit(2) from None
