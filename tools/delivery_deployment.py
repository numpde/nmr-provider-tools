"""Compose delivery from operator config and exact read-only secret grants.

The config/secrets pattern follows nmr-api-v1 compose/run.secrets.yml and Magnet
compose/provider.yml. Managed state belongs to Docker volumes. The only derived
daemon file lives in container tmpfs; provider_core owns its running lifecycle.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import tomli_w

from provider_core.deployment_binding import BindingDefinitionRejected, parse_deployment_bindings
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.deployment_route import NamedRouteVolume, RouteProjectionRejected, render_api_route
from provider_core.diagnostics import failure_evidence
from provider_core.failure_projection import owned_details
from provider_core.startup_file import read_startup_file
from provider_core.transport import require_unix_socket_path
from provider_core.provider_proxy import LiveProxyConfiguration
from provider_core.shared_delivery_config import SharedDeliveryConfiguration

# Both daemon and generic proxies use the installed, pip-free environment.
# repo-check: allow-module-constant
CORE_PYTHON = "/opt/provider-core/environment/bin/python"


class DeliveryDeploymentRejected(ValueError):
    """A code-owned grant refusal; arbitrary borrowed input is never its text."""


def _details(error, text, diagnostic_window):
    if type(error) in (DeliveryDeploymentRejected, BindingDefinitionRejected, RouteProjectionRejected):
        return {"message": str(error)}
    return owned_details(error)


def _path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(c in str(path) for c in ("\0", "\n", "\r", ",")):
        raise DeliveryDeploymentRejected("Delivery inputs require canonical absolute paths without mount delimiters.")
    return path


def read_definition(path):
    raw = read_startup_file(path, maximum_bytes=65536, label="delivery binding definition")
    definition = parse_deployment_bindings(raw)
    admit_listeners(definition)
    return definition, hashlib.sha256(raw).hexdigest()


def _bind(source, target):
    return {"type": "bind", "source": str(source), "target": str(target), "read_only": True,
            "bind": {"create_host_path": False}}


# silent-defaults: allow-contract -- Proxy socket borrowers mount the same volume read-only; its owner explicitly receives writes.
def _volume(name, target, *, readonly=False):
    return {"type": "volume", "source": name, "target": target, "read_only": readonly, "volume": {"nocopy": True}}


def _service(image, module, definition, stop_seconds):
    uid, gid = definition.uid, definition.gid
    return {
        "image": image, "pull_policy": "never", "entrypoint": [CORE_PYTHON, "-I", "-B", "-m", module],
        "user": f"{uid}:{gid}", "init": True, "read_only": True, "cap_drop": ["ALL"],
        "ipc": "private", "cgroup": "private", "security_opt": ["no-new-privileges:true"],
        "cpus": 2, "mem_limit": "512m", "memswap_limit": "512m", "pids_limit": 64,
        "restart": "unless-stopped", "stop_grace_period": f"{stop_seconds}s",
        "network_mode": "none", "labels": {"org.nmr-provider-tools.delivery": definition.project},
        "tmpfs": [f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}" for name in ("tmp", "run")],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3",
                    "mode": "non-blocking", "max-buffer-size": "1m"}},
    }


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


def daemon_configuration(definition):
    """Project one neutral definition into the existing SDK daemon contract."""
    configuration = {"schema_id": "provider-core.delivery.v1", "state_directory": "/state",
                     "request_seconds": definition.request_seconds, "retry_initial_seconds": definition.retry_initial_seconds,
                     "retry_max_seconds": definition.retry_max_seconds, "limits": definition.limits, "bindings": []}
    listeners = admit_listeners(definition)
    for binding in definition.bindings:
        key, listener = listeners[binding.name]
        api = dict(binding.api)
        for field in ("credential_file", "ca_file"):
            if field in api:
                api[field] = "/grants" + api[field]
        configuration["bindings"].append({"socket_path": listener, "socket_group_id": definition.gid,
                                           "proxy_socket": f"/routes/{key}/proxy.sock", "api": api})
    return configuration


def render(definition, source, image, checksum):
    if definition.uid != os.geteuid() or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise DeliveryDeploymentRejected("Delivery rendering requires its installing nonzero UID and an immutable image ID.")
    source = _path(source)
    listeners = admit_listeners(definition)
    queue = definition.project + "-queue"
    services, networks, volumes = {}, {}, {queue: {"name": queue}}
    daemon = _service(image, "provider_core.delivery_main", definition, 60)
    daemon["entrypoint"] = [CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"]
    daemon["command"] = ["run", "--definition", "/input/delivery.toml", "--sha256", checksum]
    daemon["healthcheck"] = {
        "test": ["CMD", CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py", "health",
                 "--definition", "/input/delivery.toml", "--sha256", checksum],
        "interval": "5s", "timeout": "3s", "retries": 3, "start_period": "5s",
    }
    daemon["depends_on"] = {"init": {"condition": "service_completed_successfully"}}
    daemon["volumes"] = [_bind(source, "/input/delivery.toml"), _volume(queue, "/state")]
    roots = [("queue", queue, "/volumes/queue")]
    for binding in definition.bindings:
        key, listener = listeners[binding.name]
        route = render_api_route(binding, image=image, interpreter=CORE_PYTHON,
                                 namespace=definition.project, uid=definition.uid, gid=definition.gid,
                                 storage=NamedRouteVolume(), input_sources={})
        if set(services) & set(route.compose["services"]):
            raise DeliveryDeploymentRejected("Delivery proxy identities collide; no ambiguous recipe was published.")
        for proxy in route.compose["services"].values():
            proxy["labels"]["org.nmr-provider-tools.delivery"] = definition.project
        services.update(route.compose["services"])
        networks.update(route.compose["networks"])
        volumes.update(route.compose["volumes"])
        volumes[binding.ipc_volume] = {"name": binding.ipc_volume}
        roots.append(("ipc", binding.ipc_volume, "/volumes/ipc-" + key))
        daemon["volumes"].extend((_volume(binding.ipc_volume, "/ipc/" + key), route.client_mount))
        daemon["depends_on"].update(route.client_depends_on)
    trust_paths = {binding.api[field] for binding in definition.bindings for field in ("credential_file", "ca_file") if field in binding.api}
    daemon["volumes"].extend(_bind(path, "/grants" + path) for path in sorted(trust_paths))
    initializer = _service(image, "provider_core.delivery_main", definition, 10)
    initializer.update(user="0:0", restart="no", cap_add=["CHOWN"],
                       entrypoint=[CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"],
                       command=["initialize", "--uid", str(definition.uid), "--gid", str(definition.gid)],
                       volumes=[_volume(volume, target) for kind, volume, target in roots])
    for kind, volume, target in roots:
        initializer["command"].extend(("--" + kind, target))
    services.update(delivery=daemon, init=initializer)
    return {"name": definition.project, "services": services, "networks": networks, "volumes": volumes}


def render_shared(path, source, image):
    """Project the one operator file into independently managed proxy and outbox projects."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise DeliveryDeploymentRejected("Shared deployment requires an immutable image ID.")
    try:
        document = tomllib.loads(read_startup_file(path, maximum_bytes=65536, label="shared settings").decode("utf-8"))
        host, route = document["deployment"], document["api_proxy"]["route"]
        project, uid, proxy_uid, gid, request_seconds = host["project"], host["uid"], host["proxy_uid"], host["gid"], host["request_seconds"]
        mode, interface = route["mode"], route["interface"]
        if not (type(project) is str and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project) is not None
                and type(uid) is int and uid == os.geteuid() and type(proxy_uid) is int and 0 < proxy_uid and proxy_uid != uid
                and type(gid) is int and 0 < gid
                and type(request_seconds) in (int, float) and 0 < request_seconds < float("inf")
                and mode in ("local", "mapped", "external") and type(interface) is str):
            raise ValueError
        LiveProxyConfiguration(path, mode).read()
        SharedDeliveryConfiguration(path).read()
        if mode == "local" and (type(route["network_id"]) is not str or re.fullmatch(r"[0-9a-f]{64}", route["network_id"]) is None):
            raise ValueError
        # silent-defaults: allow-contract -- Only external mode grants a resolver file.
        resolver = _path(route["resolver_file"]) if mode == "external" else None
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise DeliveryDeploymentRejected("Shared deployment settings require the documented host, route, proxy and outbox inputs.") from None
    source = _path(source)
    config_path = "/settings/" + source.name
    owner = SimpleNamespace(uid=uid, gid=gid, project=project)
    proxy_volume, intake_volume, queue_volume = project + "-proxy-socket", project + "-outbox-socket", project + "-queue"

    proxy_owner = SimpleNamespace(uid=proxy_uid, gid=gid, project=project + "-proxy")
    proxy = _service(image, "provider_core.provider_proxy", proxy_owner, 15)
    proxy["command"] = ["--mode", mode, "--interface", interface, "--socket", "/route/proxy.sock",
                        "--ready-file", "/route/ready", "--status-directory", "/run", "--socket-group-id", str(gid),
                        "--live-config", config_path]
    proxy["volumes"] = [_bind(source.parent, "/settings"), _volume(proxy_volume, "/route")]
    proxy["depends_on"] = {"init": {"condition": "service_completed_successfully"}}
    proxy["healthcheck"] = {"test": ["CMD", CORE_PYTHON, "-I", "-B", "-m", "provider_core.process_status",
                                      "/run/status.json", "proxy"], "interval": "5s", "timeout": "2s", "retries": 3}
    networks = {}
    if mode == "local":
        del proxy["network_mode"]
        proxy["networks"] = ["api"]
        networks["api"] = {"external": True, "name": route["network_id"]}
    else:
        proxy["network_mode"] = "host"
    if resolver is not None:
        proxy["volumes"].append(_bind(resolver, "/etc/resolv.conf"))
    proxy_init = _service(image, "provider_core.deployment_route", proxy_owner, 10)
    proxy_init.update(user="0:0", restart="no", cap_add=["CHOWN"],
                      command=["initialize", "--uid", str(proxy_uid), "--gid", str(gid), "--shared-group"],
                      volumes=[_volume(proxy_volume, "/route")])
    proxy_project = {"name": project + "-proxy", "services": {"proxy": proxy, "init": proxy_init},
                     "networks": networks, "volumes": {proxy_volume: {"name": proxy_volume}}}

    outbox = _service(image, "provider_core.delivery_main", owner, 60)
    outbox["command"] = ["--shared-config", config_path, "--state-directory", "/state", "--socket", "/intake/http.sock",
                         "--socket-group-id", str(gid), "--proxy-socket", "/proxy/proxy.sock",
                         "--request-seconds", str(request_seconds), "--status-directory", "/run"]
    outbox["volumes"] = [_bind(source.parent, "/settings"), _volume(queue_volume, "/state"),
                          _volume(intake_volume, "/intake"), _volume(proxy_volume, "/proxy", readonly=True)]
    outbox["depends_on"] = {"init": {"condition": "service_completed_successfully"}}
    outbox["healthcheck"] = {"test": ["CMD", CORE_PYTHON, "-I", "-B", "-m", "provider_core.process_status",
                                       "/run/status.json", "delivery"], "interval": "5s", "timeout": "2s", "retries": 3}
    initializer = _service(image, "provider_core.delivery_main", owner, 10)
    initializer.update(user="0:0", restart="no", cap_add=["CHOWN"],
                       entrypoint=[CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"],
                       command=["initialize", "--uid", str(uid), "--gid", str(gid), "--queue", "/volumes/queue",
                                "--ipc", "/volumes/intake"],
                       volumes=[_volume(queue_volume, "/volumes/queue"), _volume(intake_volume, "/volumes/intake")])
    outbox_project = {"name": project, "services": {"delivery": outbox, "init": initializer}, "volumes": {
        queue_volume: {"name": queue_volume}, intake_volume: {"name": intake_volume},
        proxy_volume: {"external": True, "name": proxy_volume}}}
    return {"proxy": proxy_project, "outbox": outbox_project}


def runtime_definition(path, expected):
    definition, checksum = read_definition(path)
    if checksum != expected:
        raise DeliveryDeploymentRejected("Cannot start delivery: the operator definition changed after rendering; stop the project and run up with the intended definition.")
    return definition


def runtime_configuration(path, expected):
    definition = runtime_definition(path, expected)
    encoded = tomli_w.dumps(daemon_configuration(definition)).encode()
    if not len(encoded) <= 65536:
        raise DeliveryDeploymentRejected("Cannot start delivery: the derived SDK configuration exceeds its 65536-byte contract.")
    target = Path("/run/delivery.toml")
    with target.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded)
    return target


async def intake_ready(binding, listener):
    client = DeliveryClient(DeliveryEndpoint(binding.api["origin"], binding.api["provider_ref"],
                            Path(listener), socket_group_id=binding.socket_group_id),
                            exchange_seconds=1, retirement_seconds=0.2)
    try:
        # This read remains bounded even for a valid oversized legacy queue.
        # A successful response proves local intake, never remote API readiness.
        await client.status("execution_attempt:sha256:" + "0" * 64)
    finally:
        await client.retire()


async def health(definition):
    listeners = admit_listeners(definition)
    settled = await asyncio.gather(*(intake_ready(binding, listeners[binding.name][1]) for binding in definition.bindings),
                                   return_exceptions=True)
    failures = [result for result in settled if isinstance(result, BaseException)]
    if failures:
        raise BaseExceptionGroup("Delivery intake is not ready for every configured binding.", failures)


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
    renderer = commands.add_parser("render")
    renderer.add_argument("--definition", type=Path, required=True)
    renderer.add_argument("--source", type=Path, required=True)
    renderer.add_argument("--image", required=True)
    shared = commands.add_parser("render-shared")
    shared.add_argument("--definition", type=Path, required=True)
    shared.add_argument("--source", type=Path, required=True)
    shared.add_argument("--image", required=True)
    for name in ("run", "operator", "health"):
        selected = commands.add_parser(name)
        selected.add_argument("--definition", type=Path, required=True)
        selected.add_argument("--sha256", required=True)
        if name == "operator":
            selected.add_argument("arguments", nargs=argparse.REMAINDER)
    initializer = commands.add_parser("initialize")
    for name in ("uid", "gid"):
        initializer.add_argument("--" + name, type=int, required=True)
    initializer.add_argument("--queue", type=Path, required=True)
    initializer.add_argument("--ipc", type=Path, action="append", required=True)
    options = parser.parse_args()
    if options.command == "render":
        definition, checksum = read_definition(options.definition)
        plan = render(definition, options.source, options.image, checksum)
        print(json.dumps(plan, sort_keys=True, indent=2).replace("$", "$$"))
    elif options.command == "render-shared":
        print(json.dumps(render_shared(options.definition, options.source, options.image), sort_keys=True, indent=2).replace("$", "$$"))
    elif options.command == "initialize":
        initialize(options.uid, options.gid, options.queue, options.ipc)
    elif options.command == "health":
        asyncio.run(health(runtime_definition(options.definition, options.sha256)))
    else:
        path = runtime_configuration(options.definition, options.sha256)
        module = "provider_core.delivery_main"
        arguments = []
        if options.command == "operator":
            module = "provider_core.delivery_discard"
            arguments = options.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
        os.execv(CORE_PYTHON, [CORE_PYTHON, "-I", "-B", "-m", module, "--config", str(path), *arguments])


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:
        sys.stderr.write(json.dumps(failure_evidence(error, secrets=(), describe_owner=_details), separators=(",", ":")) + "\n")
        raise SystemExit(2) from None
