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
import reprlib
import stat
import sys
import tomllib
import pathlib
from types import SimpleNamespace

import tomli_w

from provider_core.deployment_binding import BindingDefinitionRejected, parse_deployment_bindings
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.deployment_route import NamedRouteVolume, RouteProjectionRejected, render_api_route
from provider_core.diagnostics import failure_evidence
from provider_core.failure_projection import owned_details
from provider_core.transport import require_unix_socket_path
from provider_core.provider_proxy import LiveProxyConfiguration
from provider_core.shared_delivery_config import SharedDeliveryConfiguration

# The image owns these helpers beside the renderer, where isolated Python needs an explicit path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

try:
    from delivery_support.docker_ids import is_full_image_id, is_full_network_id
    from delivery_support.startup_file import read_startup_file
    from delivery_support.tools import require_absolute_path, require_positive_number
finally:
    sys.path.pop(0)

# Both daemon and generic proxies use the installed application environment.
# repo-check: allow-module-constant
CORE_PYTHON = "/opt/provider-core/environment/bin/python"


class DeliveryDeploymentRejected(ValueError):
    """A code-owned grant refusal; arbitrary borrowed input is never its text."""


def _details(error, text, diagnostic_window):
    if type(error) in (DeliveryDeploymentRejected, BindingDefinitionRejected, RouteProjectionRejected):
        return {"message": str(error)}

    return owned_details(error)


def _require_mount_path(mount_path):
    try:
        absolute_mount_path = require_absolute_path(mount_path)
    except (TypeError, ValueError) as cause:
        raise DeliveryDeploymentRejected("Cannot admit a delivery input path") from cause

    if not (absolute_mount_path.resolve() == absolute_mount_path):
        raise DeliveryDeploymentRejected("The delivery mount path should be canonical with symlinks resolved")

    if not all(character not in str(absolute_mount_path) for character in ("\n", "\r", ",")):
        raise DeliveryDeploymentRejected("The delivery mount path should contain no commas or line breaks")

    return absolute_mount_path


def read_definition(path):
    raw_definition = read_startup_file(
        path,
        maximum_bytes=65536,
    )

    definition = parse_deployment_bindings(raw_definition)

    admit_listeners(definition)

    return (definition, hashlib.sha256(raw_definition).hexdigest())


def _bind(source, target):
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
        "bind": {"create_host_path": False},
    }


# silent-defaults: allow-contract -- Proxy socket borrowers mount the same volume read-only; its owner explicitly receives writes.
def _volume(name, target, *, readonly=False):
    return {
        "type": "volume",
        "source": name,
        "target": target,
        "read_only": readonly,
        "volume": {"nocopy": True},
    }


def _service(image, module, definition, stop_seconds):
    (uid, gid) = (definition.uid, definition.gid)

    return {
        "image": image,
        "pull_policy": "never",
        "entrypoint": [CORE_PYTHON, "-I", "-B", "-m", module],
        "user": f"{uid}:{gid}",
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
        "stop_grace_period": f"{stop_seconds}s",
        "network_mode": "none",
        "labels": {"org.nmr-provider-tools.delivery": definition.project},
        "tmpfs": [
            f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}"
            for name in ("tmp", "run")
        ],
        "logging": {
            "driver": "json-file",
            "options": {
                "max-size": "10m",
                "max-file": "3",
                "mode": "non-blocking",
                "max-buffer-size": "1m",
            },
        },
    }


def admit_listeners(definition):
    listeners = {}

    for binding in definition.bindings:
        ipc_key = binding.ipc_volume.removeprefix(definition.project + "-ipc-")
        socket_path = f"/ipc/{ipc_key}/{binding.socket_path.name}"

        try:
            require_unix_socket_path(socket_path)
        except ValueError as cause:
            raise DeliveryDeploymentRejected(
                "Cannot grant delivery socket: the derived daemon path must fit "
                "Linux's 107-byte limit; shorten the provider socket filename."
            ) from cause

        listeners[binding.name] = (ipc_key, socket_path)

    return listeners


def daemon_configuration(definition):
    """Project one neutral definition into the existing SDK daemon contract."""

    configuration = {
        "schema_id": "provider-core.delivery.v1",
        "state_directory": "/state",
        "request_seconds": definition.request_seconds,
        "retry_initial_seconds": definition.retry_initial_seconds,
        "retry_max_seconds": definition.retry_max_seconds,
        "limits": definition.limits,
        "bindings": [],
    }

    listeners = admit_listeners(definition)

    for binding in definition.bindings:
        (ipc_key, socket_path) = listeners[binding.name]
        api_configuration = dict(binding.api)

        for field in ("credential_file", "ca_file"):
            if field in api_configuration:
                api_configuration[field] = "/grants" + api_configuration[field]

        configuration["bindings"].append(
            {
                "socket_path": socket_path,
                "socket_group_id": definition.gid,
                "proxy_socket": f"/routes/{ipc_key}/proxy.sock",
                "api": api_configuration,
            }
        )

    return configuration


def render(definition, source, image, checksum):
    installer_uid = os.geteuid()

    if not (definition.uid == installer_uid):
        raise DeliveryDeploymentRejected(f"{definition.uid=} should match {installer_uid=}")

    if not is_full_image_id(image):
        raise DeliveryDeploymentRejected(f"{reprlib.repr(image)=} should be sha256: followed by 64 lowercase hex digits")

    source = _require_mount_path(source)
    listeners = admit_listeners(definition)
    queue_volume = definition.project + "-queue"
    services = {}
    networks = {}
    volumes = {queue_volume: {"name": queue_volume}}

    delivery_service = _service(image, "provider_core.delivery_main", definition, 60)
    delivery_service["entrypoint"] = [CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"]
    delivery_service["command"] = ["run", "--definition", "/input/delivery.toml", "--sha256", checksum]

    delivery_service["healthcheck"] = {
        "test": [
            "CMD",
            CORE_PYTHON,
            "-I",
            "-B",
            "/opt/provider-tools/delivery_deployment.py",
            "health",
            "--definition",
            "/input/delivery.toml",
            "--sha256",
            checksum,
        ],
        "interval": "5s",
        "timeout": "3s",
        "retries": 3,
        "start_period": "5s",
    }

    delivery_service["depends_on"] = {"init": {"condition": "service_completed_successfully"}}

    delivery_service["volumes"] = [
        _bind(source, "/input/delivery.toml"),
        _volume(queue_volume, "/state"),
    ]

    owned_volume_roots = [("queue", queue_volume, "/volumes/queue")]

    for binding in definition.bindings:
        (ipc_key, socket_path) = listeners[binding.name]

        api_route = render_api_route(
            binding,
            image=image,
            interpreter=CORE_PYTHON,
            namespace=definition.project,
            uid=definition.uid,
            gid=definition.gid,
            storage=NamedRouteVolume(),
            input_sources={},
        )

        duplicate_proxy_services = services.keys() & api_route.compose["services"].keys()

        if duplicate_proxy_services:
            raise DeliveryDeploymentRejected(f"Delivery proxy service names should be unique, but {duplicate_proxy_services=}")

        for proxy in api_route.compose["services"].values():
            proxy["labels"]["org.nmr-provider-tools.delivery"] = definition.project

        services.update(api_route.compose["services"])
        networks.update(api_route.compose["networks"])
        volumes.update(api_route.compose["volumes"])
        volumes[binding.ipc_volume] = {"name": binding.ipc_volume}

        owned_volume_roots.append(("ipc", binding.ipc_volume, "/volumes/ipc-" + ipc_key))

        delivery_service["volumes"].extend(
            (_volume(binding.ipc_volume, "/ipc/" + ipc_key), api_route.client_mount)
        )

        delivery_service["depends_on"].update(api_route.client_depends_on)

    trust_paths = {
        binding.api[field]
        for binding in definition.bindings
        for field in ("credential_file", "ca_file")
        if field in binding.api
    }

    delivery_service["volumes"].extend(
        _bind(trust_path, "/grants" + trust_path) for trust_path in sorted(trust_paths)
    )

    volume_initializer = _service(image, "provider_core.delivery_main", definition, 10)

    volume_initializer.update(
        user="0:0",
        restart="no",
        cap_add=["CHOWN"],
        entrypoint=[CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"],
        command=["initialize", "--uid", str(definition.uid), "--gid", str(definition.gid)],
        volumes=[_volume(volume, target) for (kind, volume, target) in owned_volume_roots],
    )

    for (kind, volume, target) in owned_volume_roots:
        volume_initializer["command"].extend(("--" + kind, target))

    services.update(delivery=delivery_service, init=volume_initializer)

    return {
        "name": definition.project,
        "services": services,
        "networks": networks,
        "volumes": volumes,
    }


def render_shared(path, source, image):
    """Project the one operator file into independently managed proxy and outbox projects."""

    if not is_full_image_id(image):
        raise DeliveryDeploymentRejected(f"Shared deployment requires {reprlib.repr(image)=} to be an immutable image ID.")

    try:
        document = tomllib.loads(
            read_startup_file(path, maximum_bytes=65536).decode("utf-8")
        )

        host = document["deployment"]
        route = document["api_proxy"]["route"]

        project = host["project"]
        uid = host["uid"]
        proxy_uid = host["proxy_uid"]
        gid = host["gid"]
        request_seconds = host["request_seconds"]
        mode = route["mode"]
        interface = route["interface"]

        if not (type(project) is (expected_type := str)):
            raise TypeError(f"{type(project)=} ≠ {expected_type=}")

        if not (re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project) is not None):
            raise ValueError

        if not (type(uid) is (expected_type := int)):
            raise TypeError(f"{type(uid)=} ≠ {expected_type=}")

        if not (uid == os.geteuid()):
            raise ValueError

        if not (type(proxy_uid) is (expected_type := int)):
            raise TypeError(f"{type(proxy_uid)=} ≠ {expected_type=}")

        if not (0 < proxy_uid and proxy_uid != uid):
            raise ValueError

        if not (type(gid) is (expected_type := int)):
            raise TypeError(f"{type(gid)=} ≠ {expected_type=}")

        if not (0 < gid):
            raise ValueError

        require_positive_number(request_seconds)

        if not (mode in ("local", "mapped", "external")):
            raise ValueError

        if not (type(interface) is (expected_type := str)):
            raise TypeError(f"{type(interface)=} ≠ {expected_type=}")

        LiveProxyConfiguration(path, mode).read()
        SharedDeliveryConfiguration(path).read()

        if mode == "local":

            if not (type((network_id := route["network_id"])) is (expected_type := str)):
                raise TypeError(f"{type(network_id)=} ≠ {expected_type=}")

            if not is_full_network_id(route["network_id"]):
                raise ValueError

        # silent-defaults: allow-contract -- Only external mode grants a resolver file.
        resolver = _require_mount_path(route["resolver_file"]) if mode == "external" else None
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise DeliveryDeploymentRejected(
            "Shared deployment settings require the documented host, route, "
            "proxy and outbox inputs."
        ) from None

    source = _require_mount_path(source)
    config_path = "/settings/" + source.name
    owner = SimpleNamespace(uid=uid, gid=gid, project=project)
    proxy_volume = project + "-proxy-socket"
    intake_volume = project + "-outbox-socket"
    queue_volume = project + "-queue"

    proxy_owner = SimpleNamespace(uid=proxy_uid, gid=gid, project=project + "-proxy")
    proxy_service = _service(image, "provider_core.provider_proxy", proxy_owner, 15)

    proxy_service["command"] = [
        "--mode",
        mode,
        "--interface",
        interface,
        "--socket",
        "/route/proxy.sock",
        "--ready-file",
        "/route/ready",
        "--status-directory",
        "/run",
        "--socket-group-id",
        str(gid),
        "--live-config",
        config_path,
    ]

    proxy_service["volumes"] = [
        _bind(source.parent, "/settings"),
        _volume(proxy_volume, "/route"),
    ]

    proxy_service["depends_on"] = {"init": {"condition": "service_completed_successfully"}}

    proxy_service["healthcheck"] = {
        "test": [
            "CMD",
            CORE_PYTHON,
            "-I",
            "-B",
            "-m",
            "provider_core.process_status",
            "/run/status.json",
            "proxy",
        ],
        "interval": "5s",
        "timeout": "2s",
        "retries": 3,
    }

    networks = {}

    if mode == "local":
        del proxy_service["network_mode"]
        proxy_service["networks"] = ["api"]
        networks["api"] = {"external": True, "name": route["network_id"]}
    else:
        proxy_service["network_mode"] = "host"

    if resolver is not None:
        proxy_service["volumes"].append(_bind(resolver, "/etc/resolv.conf"))

    proxy_initializer = _service(image, "provider_core.deployment_route", proxy_owner, 10)

    proxy_initializer.update(
        user="0:0",
        restart="no",
        cap_add=["CHOWN"],
        command=["initialize", "--uid", str(proxy_uid), "--gid", str(gid), "--shared-group"],
        volumes=[_volume(proxy_volume, "/route")],
    )

    proxy_project = {
        "name": project + "-proxy",
        "services": {"proxy": proxy_service, "init": proxy_initializer},
        "networks": networks,
        "volumes": {proxy_volume: {"name": proxy_volume}},
    }

    outbox_service = _service(image, "provider_core.delivery_main", owner, 60)

    outbox_service["command"] = [
        "--shared-config",
        config_path,
        "--state-directory",
        "/state",
        "--socket",
        "/intake/http.sock",
        "--socket-group-id",
        str(gid),
        "--proxy-socket",
        "/proxy/proxy.sock",
        "--request-seconds",
        str(request_seconds),
        "--status-directory",
        "/run",
    ]

    outbox_service["volumes"] = [
        _bind(source.parent, "/settings"),
        _volume(queue_volume, "/state"),
        _volume(intake_volume, "/intake"),
        _volume(proxy_volume, "/proxy", readonly=True),
    ]

    outbox_service["depends_on"] = {"init": {"condition": "service_completed_successfully"}}

    outbox_service["healthcheck"] = {
        "test": [
            "CMD",
            CORE_PYTHON,
            "-I",
            "-B",
            "-m",
            "provider_core.process_status",
            "/run/status.json",
            "delivery",
        ],
        "interval": "5s",
        "timeout": "2s",
        "retries": 3,
    }

    outbox_initializer = _service(image, "provider_core.delivery_main", owner, 10)

    outbox_initializer.update(
        user="0:0",
        restart="no",
        cap_add=["CHOWN"],
        entrypoint=[CORE_PYTHON, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"],
        command=[
            "initialize",
            "--uid",
            str(uid),
            "--gid",
            str(gid),
            "--queue",
            "/volumes/queue",
            "--ipc",
            "/volumes/intake",
        ],
        volumes=[
            _volume(queue_volume, "/volumes/queue"),
            _volume(intake_volume, "/volumes/intake"),
        ],
    )

    outbox_project = {
        "name": project,
        "services": {"delivery": outbox_service, "init": outbox_initializer},
        "volumes": {
            queue_volume: {"name": queue_volume},
            intake_volume: {"name": intake_volume},
            proxy_volume: {"external": True, "name": proxy_volume},
        },
    }

    return {"proxy": proxy_project, "outbox": outbox_project}


def runtime_definition(path, expected):
    (definition, actual_checksum) = read_definition(path)

    if actual_checksum != expected:
        raise DeliveryDeploymentRejected(
            "Cannot start delivery: the operator definition changed after rendering; "
            "stop the project and run up with the intended definition."
        )

    return definition


def runtime_configuration(path, expected):
    definition = runtime_definition(path, expected)
    encoded_configuration = tomli_w.dumps(daemon_configuration(definition)).encode()

    if not (len(encoded_configuration) <= 65536):
        raise DeliveryDeploymentRejected(
            "Cannot start delivery: the derived SDK configuration exceeds "
            "its 65536-byte contract."
        )

    configuration_path = pathlib.Path("/run/delivery.toml")

    with configuration_path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded_configuration)

    return configuration_path


async def intake_ready(binding, listener):
    endpoint = DeliveryEndpoint(
        binding.api["origin"],
        binding.api["provider_ref"],
        pathlib.Path(listener),
        socket_group_id=binding.socket_group_id,
    )

    client = DeliveryClient(
        endpoint,
        exchange_seconds=1,
        retirement_seconds=0.2,
    )

    try:
        # A successful response proves local intake, never remote API readiness.
        # This read remains bounded even for a valid oversized legacy queue.
        await client.status("execution_attempt:sha256:" + "0" * 64)
    finally:
        await client.retire()


async def health(definition):
    listeners = admit_listeners(definition)

    intake_results = await asyncio.gather(
        *(
            intake_ready(binding, listeners[binding.name][1])
            for binding in definition.bindings
        ),
        return_exceptions=True,
    )

    intake_failures = [
        intake_result for intake_result in intake_results if isinstance(intake_result, BaseException)
    ]

    if intake_failures:
        raise BaseExceptionGroup(
            "Delivery intake is not ready for every configured binding.",
            intake_failures,
        )


def initialize(uid, gid, queue, ipc):
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise DeliveryDeploymentRejected(
            "Volume initialization requires root with CHOWN and explicit "
            "nonzero target UID/GID."
        )

    for (target, mode) in [(queue, 0o700), *((ipc_root, 0o710) for ipc_root in ipc)]:
        volume_root = _require_mount_path(target)
        observed_metadata = volume_root.lstat()

        # todo: use format/pattern from elsewhere
        if not stat.S_ISDIR(observed_metadata.st_mode):
            raise DeliveryDeploymentRejected("Delivery volume root must be a real directory.")

        if (
            observed_metadata.st_uid,
            observed_metadata.st_gid,
            stat.S_IMODE(observed_metadata.st_mode),
        ) == (uid, gid, mode):
            continue

        if observed_metadata.st_uid != 0 or any(volume_root.iterdir()):
            raise DeliveryDeploymentRejected(
                "Refusing to change an existing delivery volume with "
                "unexpected ownership, mode or contents."
            )

        volume_fd = os.open(volume_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

        try:
            os.fchmod(volume_fd, mode)
            os.fchown(volume_fd, uid, gid)
            os.fsync(volume_fd)
        finally:
            os.close(volume_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    renderer = commands.add_parser("render")

    renderer.add_argument("--definition", type=pathlib.Path, required=True)
    renderer.add_argument("--source", type=pathlib.Path, required=True)
    renderer.add_argument("--image", required=True)

    shared_renderer = commands.add_parser("render-shared")

    shared_renderer.add_argument("--definition", type=pathlib.Path, required=True)
    shared_renderer.add_argument("--source", type=pathlib.Path, required=True)
    shared_renderer.add_argument("--image", required=True)

    for name in ("run", "operator", "health"):
        operation_parser = commands.add_parser(name)

        operation_parser.add_argument("--definition", type=pathlib.Path, required=True)
        operation_parser.add_argument("--sha256", required=True)

        if name == "operator":
            operation_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    initializer_parser = commands.add_parser("initialize")

    for name in ("uid", "gid"):
        initializer_parser.add_argument("--" + name, type=int, required=True)

    initializer_parser.add_argument("--queue", type=pathlib.Path, required=True)
    initializer_parser.add_argument("--ipc", type=pathlib.Path, action="append", required=True)

    options = parser.parse_args()

    if options.command == "render":
        (definition, checksum) = read_definition(options.definition)
        compose_plan = render(definition, options.source, options.image, checksum)

        print(json.dumps(compose_plan, sort_keys=True, indent=2).replace("$", "$$"))
    elif options.command == "render-shared":
        print(
            json
            .dumps(
                render_shared(options.definition, options.source, options.image),
                sort_keys=True,
                indent=2,
            )
            .replace("$", "$$"),
        )
    elif options.command == "initialize":
        initialize(options.uid, options.gid, options.queue, options.ipc)
    elif options.command == "health":
        asyncio.run(health(runtime_definition(options.definition, options.sha256)))
    else:
        configuration_path = runtime_configuration(options.definition, options.sha256)
        module = "provider_core.delivery_main"
        arguments = []

        if options.command == "operator":
            module = "provider_core.delivery_discard"
            arguments = options.arguments

            if arguments[:1] == ["--"]:
                arguments = arguments[1:]

        os.execv(
            CORE_PYTHON,
            [
                CORE_PYTHON,
                "-I",
                "-B",
                "-m",
                module,
                "--config",
                str(configuration_path),
                *arguments,
            ],
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as error:
        sys.stderr.write(
            json.dumps(
                failure_evidence(error, secrets=(), describe_owner=_details),
                separators=(",", ":"),
            ) + "\n"
        )

        raise SystemExit(2) from None
