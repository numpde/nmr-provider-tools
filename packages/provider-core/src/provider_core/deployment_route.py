"""Project one selected API route into private proxy and client grants.

The proxy recipe moves tools/delivery_deployment.py::_proxy. Binding admission
owns API and route semantics; this module grants the chosen image, identity and
storage to that fixed proxy. Credential capture and scientific routes stay with
their existing owners. Rendering does not read the granted input files.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from provider_core.deployment_binding import BindingDefinitionRejected, DeploymentBinding, input_paths, load_deployment_bindings
from provider_core.diagnostics import failure_evidence
from provider_core.failure_projection import owned_details


class RouteProjectionRejected(ValueError):
    """A safe refusal of an explicit image, identity, path or storage grant."""


@dataclass(frozen=True, slots=True)
class NamedRouteVolume:
    """Grant a private engine volume, initialized for this runtime identity."""


@dataclass(frozen=True, slots=True)
class RouteDirectory:
    """Grant a private directory already prepared by the caller's host owner."""

    path: Path


@dataclass(frozen=True, slots=True)
class ApiRouteProjection:
    """Compose roles plus the exact grants borrowed by the API client."""

    compose: dict
    client_socket: str
    client_mount: dict
    client_depends_on: dict


def _path(value):
    path = Path(value)
    if (path.anchor != "/" or ".." in path.parts or str(path) != str(value)
            or path == Path("/") or any(ord(character) < 32 or ord(character) == 127 or character == "," for character in str(path))):
        raise RouteProjectionRejected("Cannot grant API route input: require a normalized absolute path below root without control characters or mount delimiters.")
    return str(path)


def _identity(uid, gid):
    if not all(type(value) is int and 1 <= value <= 4294967294 for value in (uid, gid)):
        raise RouteProjectionRejected("Cannot grant API route identity: require explicit nonzero Linux UID and GID.")


def _volume(name, target, *, readonly):
    return {"type": "volume", "source": name, "target": target,
            "read_only": readonly, "volume": {"nocopy": True}}


def _bind(source, target, *, readonly):
    return {"type": "bind", "source": _path(source), "target": target,
            "read_only": readonly, "bind": {"create_host_path": False}}


def _service(image, interpreter, namespace, key, uid, gid):
    return {
        "image": image, "pull_policy": "never", "user": f"{uid}:{gid}",
        "entrypoint": [interpreter, "-I", "-B", "-m", "provider_core.provider_proxy"],
        "init": True, "read_only": True, "cap_drop": ["ALL"],
        "ipc": "private", "cgroup": "private", "security_opt": ["no-new-privileges:true"],
        "cpus": 2, "mem_limit": "512m", "memswap_limit": "512m", "pids_limit": 64,
        "restart": "unless-stopped", "stop_grace_period": "15s", "network_mode": "none",
        "labels": {"org.nmr-provider-tools.route": namespace, "org.nmr-provider-tools.route-role": "proxy",
                   "org.nmr-provider-tools.binding": key},
        "tmpfs": [f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}" for name in ("tmp", "run")],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3",
                    "mode": "non-blocking", "max-buffer-size": "1m"}},
    }


def _named_initializer(image, interpreter, namespace, key, uid, gid, volume):
    service = _service(image, interpreter, namespace, key, uid, gid)
    service.update(user="0:0", restart="no", cap_add=["CHOWN"], stop_grace_period="10s",
                   entrypoint=[interpreter, "-I", "-B", "-m", "provider_core.deployment_route"],
                   command=["initialize", "--uid", str(uid), "--gid", str(gid)],
                   volumes=[_volume(volume, "/route", readonly=False)])
    service["labels"]["org.nmr-provider-tools.route-role"] = "initializer"
    # Root initializes only the route volume; its own scratch needs no provider identity.
    service["tmpfs"] = [f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700" for name in ("tmp", "run")]
    return service


def _proxy_route(service, binding, interpreter, input_sources, role):
    route = binding.route
    parsed = urlsplit(binding.api["origin"])
    port = parsed.port
    # HTTPS owns port 443 when the admitted canonical origin omits it.
    if port is None:
        port = 443
    command = ["--mode", route["mode"], "--interface", route["interface"], "--log-level", route["log_level"],
               "--socket", "/route/proxy.sock", "--ready-file", "/route/ready", "--status-directory", "/run",
               "--authority", f"{parsed.hostname}:{port}"]
    networks = {}
    if route["mode"] == "local":
        del service["network_mode"]
        service["networks"] = [role]
        networks[role] = {"external": True, "name": route["network_id"]}
    else:
        service["network_mode"] = "host"
    if route["mode"] == "external":
        resolver = route["resolver_file"]
        if resolver in input_sources:
            resolver = input_sources[resolver]
        service["volumes"].append(_bind(resolver, "/etc/resolv.conf", readonly=True))
    else:
        for address in route["addresses"]:
            command.extend(("--address", address))
    service["command"] = command
    service["healthcheck"] = {"test": ["CMD", interpreter, "-I", "-B", "-m", "provider_core.process_status",
                                         "/run/status.json", "proxy"],
                              "interval": "5s", "timeout": "2s", "retries": 3, "start_period": "5s"}
    return networks


def render_api_route(binding, *, image, interpreter, namespace, uid, gid, storage, input_sources):
    """Render an admitted binding without reading secrets or preparing host paths.

    Captured input paths relocate only existing grants. Frozen launchers supply
    their capture map; an explicit empty map uses the original admitted sources.
    The consumer drains its API owner before retiring the returned proxy roles.
    """
    if type(binding) is not DeploymentBinding:
        raise RouteProjectionRejected("Cannot render API route: select an admitted DeploymentBinding.")
    if type(image) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise RouteProjectionRejected("Cannot render API route: select an immutable full image ID.")
    if type(namespace) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", namespace) is None:
        raise RouteProjectionRejected("Cannot render API route: select a lowercase Compose namespace containing letters, digits, hyphens or underscores.")
    _identity(uid, gid)
    interpreter = _path(interpreter)
    if type(input_sources) is not dict or not set(input_sources) <= set(input_paths((binding,))):
        raise RouteProjectionRejected("Cannot relocate API route inputs: only files granted by the selected binding may be mapped.")
    for target in input_sources.values():
        _path(target)
    key = binding.ipc_volume.removeprefix(binding.project + "-ipc-")
    role = "api-" + key[:16]
    service = _service(image, interpreter, namespace, key, uid, gid)
    services, volumes = {role: service}, {}
    target = "/routes/" + key
    if type(storage) is NamedRouteVolume:
        volume = namespace + "-route-" + key
        if len(namespace) > 63:
            # Preserve existing delivery grants while bounding storage for full provider project names.
            volume = "r-" + hashlib.sha256(namespace.encode("ascii")).hexdigest() + "-" + key
        volumes[volume] = {"name": volume}
        service["volumes"] = [_volume(volume, "/route", readonly=False)]
        client_mount = _volume(volume, target, readonly=True)
        initializer = "api-init-" + key[:16]
        services[initializer] = _named_initializer(image, interpreter, namespace, key, uid, gid, volume)
        service["depends_on"] = {initializer: {"condition": "service_completed_successfully"}}
    elif type(storage) is RouteDirectory:
        service["volumes"] = [_bind(storage.path, "/route", readonly=False)]
        client_mount = _bind(storage.path, target, readonly=True)
    else:
        raise RouteProjectionRejected("Cannot render API route: explicitly grant a named route volume or a caller-prepared private directory.")
    networks = _proxy_route(service, binding, interpreter, input_sources, role)
    return ApiRouteProjection({"services": services, "networks": networks, "volumes": volumes},
                              target + "/proxy.sock", client_mount, {role: {"condition": "service_healthy"}})


def initialize_route(uid, gid, path):
    """Initialize an empty root-owned volume or preserve an exact private root.

    CHOWN-only root cannot open an existing private provider root. Preserve an
    exact real root from metadata alone; ownership changes use an admitted
    descriptor. A populated or differently owned root is never repaired in place.
    """
    _identity(uid, gid)
    if os.geteuid() != 0:
        raise RouteProjectionRejected("Cannot initialize API route volume: root with CHOWN and explicit target identity is required.")
    path = Path(_path(path))
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise RouteProjectionRejected("Cannot initialize API route volume: its root must be a real directory.")
    if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, 0o700):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        observed = os.fstat(descriptor)
        if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) != (uid, gid, 0o700):
            if observed.st_uid != 0 or os.listdir(descriptor):
                raise RouteProjectionRejected("Cannot initialize API route volume: unexpected ownership, permissions or retained contents require operator inspection.")
            os.fchmod(descriptor, 0o700)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
    except BaseException as failure:
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            raise BaseExceptionGroup("API route initialization and descriptor closure both failed.", (failure, cleanup)) from None
        raise
    else:
        os.close(descriptor)


def _details(error, text, diagnostic_window):
    if type(error) in (RouteProjectionRejected, BindingDefinitionRejected):
        return {"message": str(error)}
    return owned_details(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    renderer = commands.add_parser("render")
    renderer.add_argument("--definition", type=Path, required=True)
    renderer.add_argument("--binding", required=True)
    for name in ("image", "interpreter", "namespace"):
        renderer.add_argument("--" + name, required=True)
    for command in (renderer, commands.add_parser("initialize")):
        for name in ("uid", "gid"):
            command.add_argument("--" + name, type=int, required=True)
    storage = renderer.add_mutually_exclusive_group(required=True)
    storage.add_argument("--named-volume", action="store_true")
    storage.add_argument("--directory", type=Path)
    renderer.add_argument("--input-source", nargs=2, action="append")
    options = parser.parse_args()
    try:
        if options.action == "initialize":
            initialize_route(options.uid, options.gid, Path("/route"))
        else:
            selected = load_deployment_bindings(options.definition).selected(options.binding)
            sources = {}
            if options.input_source is not None:
                sources = dict(options.input_source)
                if len(sources) != len(options.input_source):
                    raise RouteProjectionRejected("Cannot relocate API route inputs: each original path must be mapped once.")
            if options.named_volume:
                grant = NamedRouteVolume()
            else:
                grant = RouteDirectory(options.directory)
            projection = render_api_route(selected, image=options.image, interpreter=options.interpreter,
                                          namespace=options.namespace, uid=options.uid, gid=options.gid,
                                          storage=grant, input_sources=sources)
            print(json.dumps(asdict(projection), ensure_ascii=True, sort_keys=True))
        return 0
    # silent-defaults: allow-exception-outcome -- The CLI reports safe retained evidence and refuses the requested projection or initialization.
    except (Exception, BaseExceptionGroup) as failure:
        print(json.dumps({"operation": options.action + " API route", "failure": failure_evidence(
            failure, secrets=(), describe_owner=_details,
        )}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
