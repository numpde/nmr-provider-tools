"""Project one selected API route into private proxy and client grants.

The proxy recipe moves operations/delivery/image/delivery_deployment.py::_proxy. Binding admission
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
import reprlib
import stat
import sys
from dataclasses import asdict, dataclass
import pathlib
from urllib.parse import urlsplit

from provider_core.deployment_binding import BindingDefinitionRejected, DeploymentBinding, input_paths, load_deployment_bindings
from provider_core.deployment_errors import RouteProjectionRejected
from provider_core.type_requirements import require_type
from provider_core.validation.upstream.docker_ids import is_full_image_id


@dataclass(frozen=True, slots=True)
class NamedRouteVolume:
    """Grant a private engine volume, initialized for this runtime identity."""


@dataclass(frozen=True, slots=True)
class RouteDirectory:
    """Grant a private directory already prepared by the caller's host owner."""

    path: pathlib.Path


@dataclass(frozen=True, slots=True)
class ApiRouteProjection:
    """Compose roles plus the exact grants borrowed by the API client."""

    compose: dict
    client_socket: str
    client_mount: dict
    client_depends_on: dict


def _require_route_path(route_path):
    absolute_route_path = pathlib.Path(route_path)

    # Docker bind arguments treat commas as mount separators
    if (
        (absolute_route_path.anchor != "/") or
        (".." in absolute_route_path.parts) or
        (str(absolute_route_path) != str(route_path)) or
        (absolute_route_path == pathlib.Path("/")) or
        any(((ord(character) < 32) or (ord(character) == 127) or (character == ",")) for character in str(absolute_route_path))
    ):
        raise RouteProjectionRejected(f"{reprlib.repr(route_path)=} should be a normalized absolute path below root without control characters or commas")

    return str(absolute_route_path)


def _identity(uid, gid):
    require_type(
        uid,
        int,
        error_factory=lambda diagnostic: RouteProjectionRejected("Cannot grant API route identity: " + diagnostic),
    )

    # Exclude root and the all-ones UID/GID reserved by ownership syscalls
    (min_linux_id, max_linux_id) = (1, 4294967294)

    if not (min_linux_id <= uid <= max_linux_id):
        raise RouteProjectionRejected(f"{uid=} should be between {min_linux_id=} and {max_linux_id=}")

    require_type(
        gid,
        int,
        error_factory=lambda diagnostic: RouteProjectionRejected("Cannot grant API route identity: " + diagnostic),
    )

    if not (min_linux_id <= gid <= max_linux_id):
        raise RouteProjectionRejected(f"{gid=} should be between {min_linux_id=} and {max_linux_id=}")


def _volume(name, target, *, readonly):
    return {
        "type": "volume",
        "source": name,
        "target": target,
        "read_only": readonly,
        "volume": {"nocopy": True}
    }


def _bind(source, target, *, readonly):
    return {
        "type": "bind",
        "source": _require_route_path(source),
        "target": target,
        "read_only": readonly,
        "bind": {"create_host_path": False}
    }


def _service(image, interpreter, namespace, key, uid, gid):
    return {
        "image": image,
        "pull_policy": "never",
        "user": f"{uid}:{gid}",  # machine-string: Docker Compose parses the UID:GID container identity syntax
        "entrypoint": [interpreter, "-I", "-B", "-m", "provider_core.provider_proxy"],
        "init": True,
        "read_only": True,
        "cap_drop": ["ALL"],
        "ipc": "private",
        "cgroup": "private",
        "security_opt": ["no-new-privileges:true"],
        "cpus": 2,
        "mem_limit": "512m",
        "memswap_limit": "512m",
        "pids_limit": 64,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "network_mode": "none",
        "labels": {
            "org.nmr-provider-tools.route": namespace,
            "org.nmr-provider-tools.route-role": "proxy",
            "org.nmr-provider-tools.binding": key
        },
        "tmpfs": [
            f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}"  # machine-string: Docker parses this tmpfs mount specification
            for name in ("tmp", "run")
        ],
        "logging": {
            "driver": "json-file",
            "options": {
                "max-size": "10m",
                "max-file": "3",
                "mode": "non-blocking",
                "max-buffer-size": "1m"
            }
        },
    }


def _named_initializer(image, interpreter, namespace, key, uid, gid, volume):
    service = _service(image, interpreter, namespace, key, uid, gid)

    service.update(
        user="0:0",
        restart="no",
        cap_add=["CHOWN"],
        stop_grace_period="10s",
        entrypoint=[interpreter, "-I", "-B", "-m", "provider_core.deployment_route"],
        command=["initialize", "--uid", str(uid), "--gid", str(gid)],
        volumes=[_volume(volume, "/route", readonly=False)]
    )

    service["labels"]["org.nmr-provider-tools.route-role"] = "initializer"

    # Root initializes only the route volume; its own scratch needs no provider identity.
    service["tmpfs"] = [
        f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700"  # machine-string: Docker parses this initializer tmpfs mount specification
        for name in ("tmp", "run")
    ]

    return service


def _proxy_route(service, binding, interpreter, input_sources, role):
    route = binding.route
    parsed = urlsplit(binding.api["origin"])
    port = parsed.port

    # HTTPS owns port 443 when the admitted canonical origin omits it.
    if port is None:
        port = 443

    command = [
        "--mode",
        route["mode"],
        "--interface",
        route["interface"],
        "--log-level",
        route["log_level"],
        "--socket",
        "/route/proxy.sock",
        "--ready-file",
        "/route/ready",
        "--status-directory",
        "/run",
        "--authority",
        f"{parsed.hostname}:{port}"  # machine-string: the route initializer parses this host authority as an argv value
    ]

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

    service["healthcheck"] = {
        "test": [
            "CMD",
            interpreter,
            "-I",
            "-B",
            "-m",
            "provider_core.process_status",
            "/run/status.json",
            "proxy"
        ],
        "interval": "5s",
        "timeout": "2s",
        "retries": 3,
        "start_period": "5s"
    }

    return networks


def render_api_route(binding, *, image, interpreter, namespace, uid, gid, storage, input_sources, service_name=None):
    """Render an admitted binding without reading secrets or preparing host paths.

    Captured input paths relocate only existing grants. Frozen launchers supply
    their capture map; an explicit empty map uses the original admitted sources.
    The consumer drains its API owner before retiring the returned proxy roles.
    Omitted service_name preserves delivery's identity-derived service roles.
    """

    require_type(binding, DeploymentBinding, error_factory=RouteProjectionRejected)

    require_type(
        image,
        str,
        error_factory=lambda diagnostic: RouteProjectionRejected("Cannot render API route: " + diagnostic),
    )

    if not is_full_image_id(image):
        raise RouteProjectionRejected(f"Cannot render API route: {reprlib.repr(image)=} should be an immutable full image ID.")

    require_type(
        namespace,
        str,
        error_factory=lambda diagnostic: RouteProjectionRejected("Cannot render API route: " + diagnostic),
    )

    expected_name_pattern = r"[a-z0-9][a-z0-9_-]*"

    if re.fullmatch(expected_name_pattern, namespace) is None:
        raise RouteProjectionRejected(f"{reprlib.repr(namespace)=} should match {expected_name_pattern=}")

    if service_name is not None:
        require_type(
            service_name,
            str,
            error_factory=lambda diagnostic: RouteProjectionRejected("Cannot render API route: " + diagnostic),
        )

        if re.fullmatch(expected_name_pattern, service_name) is None:
            raise RouteProjectionRejected(f"{reprlib.repr(service_name)=} should match {expected_name_pattern=}")

    _identity(uid, gid)
    interpreter = _require_route_path(interpreter)

    require_type(
        input_sources,
        dict,
        error_factory=lambda diagnostic: RouteProjectionRejected("Cannot relocate API route inputs: " + diagnostic),
    )

    provided_input_paths = set(input_sources)
    allowed_input_paths = set(input_paths((binding,)))

    if not (provided_input_paths <= allowed_input_paths):
        raise RouteProjectionRejected(f"{reprlib.repr(provided_input_paths)=} should be a subset of {reprlib.repr(allowed_input_paths)=}")

    for target in input_sources.values():
        _require_route_path(target)

    key = binding.ipc_volume.removeprefix(binding.project + "-ipc-")
    role = "api-" + key[:16]

    if service_name is not None:
        role = service_name

    service = _service(image, interpreter, namespace, key, uid, gid)
    (services, volumes) = ({role: service}, {})
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

        if service_name is not None:
            initializer = service_name + "-init"

        services[initializer] = _named_initializer(image, interpreter, namespace, key, uid, gid, volume)
        service["depends_on"] = {initializer: {"condition": "service_completed_successfully"}}
    elif type(storage) is RouteDirectory:
        service["volumes"] = [_bind(storage.path, "/route", readonly=False)]
        client_mount = _bind(storage.path, target, readonly=True)
    else:
        require_type(storage, (NamedRouteVolume, RouteDirectory), error_factory=RouteProjectionRejected)

    networks = _proxy_route(service, binding, interpreter, input_sources, role)

    return ApiRouteProjection(
        {"services": services, "networks": networks, "volumes": volumes},
        target + "/proxy.sock",
        client_mount,
        {role: {"condition": "service_healthy"}}
    )


# silent-defaults: allow-contract -- Existing private routes remain private unless the caller explicitly requests the shared group grant.
def initialize_route(uid, gid, path, *, shared_group=False):
    """Initialize an empty root-owned volume or preserve an exact private root.

    CHOWN-only root cannot open an existing private provider root. Preserve an
    exact real root from metadata alone; ownership changes use an admitted
    descriptor. A populated or differently owned root is never repaired in place.
    """
    _identity(uid, gid)

    if not ((effective_uid := os.geteuid()) == 0):
        raise RouteProjectionRejected(f"{effective_uid=} should be zero to initialize the route volume")

    route_root = pathlib.Path(_require_route_path(path))

    # silent-defaults: allow-contract -- Directory access follows the explicitly selected shared or private grant.
    mode = 0o710 if shared_group else 0o700

    observed = route_root.lstat()

    if not stat.S_ISDIR(observed.st_mode):
        raise RouteProjectionRejected(f"{route_root=}: {stat.S_IFMT(observed.st_mode)=:#o} should be {stat.S_IFDIR=:#o}")

    if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, mode):
        return

    descriptor = os.open(route_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    try:
        observed = os.fstat(descriptor)

        if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) != (uid, gid, mode):
            if not (observed.st_uid == 0):
                raise RouteProjectionRejected(f"{route_root=}: {observed.st_uid=} should be zero before initialization")

            if not (len(retained_entries := os.listdir(descriptor)) == 0):
                raise RouteProjectionRejected(f"{route_root=}: {reprlib.repr(retained_entries)=} should be empty before initialization")

            os.fchmod(descriptor, mode)
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


def main():
    from provider_core.diagnostics import failure_evidence

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    renderer = commands.add_parser("render")

    renderer.add_argument("--definition", type=pathlib.Path, required=True)
    renderer.add_argument("--binding", required=True)
    renderer.add_argument("--service-name")

    for name in ("image", "interpreter", "namespace"):
        renderer.add_argument("--" + name, required=True)

    initializer = commands.add_parser("initialize")

    initializer.add_argument("--shared-group", action="store_true")

    for command in (renderer, initializer):
        for name in ("uid", "gid"):
            command.add_argument("--" + name, type=int, required=True)

    storage = renderer.add_mutually_exclusive_group(required=True)

    storage.add_argument("--named-volume", action="store_true")
    storage.add_argument("--directory", type=pathlib.Path)
    renderer.add_argument("--input-source", nargs=2, action="append")
    options = parser.parse_args()

    try:
        if options.action == "initialize":
            initialize_route(options.uid, options.gid, pathlib.Path("/route"), shared_group=options.shared_group)
        else:
            selected = load_deployment_bindings(options.definition).selected(options.binding)
            sources = {}

            if options.input_source is not None:
                sources = dict(options.input_source)

                if len(sources) != len(options.input_source):
                    raise RouteProjectionRejected(f"{len(sources)=} should match {len(options.input_source)=}")

            if options.named_volume:
                grant = NamedRouteVolume()
            else:
                grant = RouteDirectory(options.directory)

            projection = render_api_route(
                selected,
                image=options.image,
                interpreter=options.interpreter,
                namespace=options.namespace,
                uid=options.uid,
                gid=options.gid,
                storage=grant,
                input_sources=sources,
                service_name=options.service_name
            )

            print(json.dumps(asdict(projection), ensure_ascii=True, sort_keys=True))

        return 0
    # silent-defaults: allow-exception-outcome -- The CLI reports safe retained evidence and refuses the requested projection or initialization.
    except (Exception, BaseExceptionGroup) as failure:
        print(
            json.dumps(
                {
                    "operation": options.action + " API route",
                    "failure": failure_evidence(
                        failure,
                        secrets=(),
                    )
                },
                ensure_ascii=True
            ),
            file=sys.stderr
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
