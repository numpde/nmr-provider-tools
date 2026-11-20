"""Read the single operator-owned API, route and delivery grant definition.

Providers select one binding while the host installs the complete set. Their
frozen runtime configuration is derived from this input; scientific configuration
and producer checkpoints do not belong in this document. Loading describes and
admits grants without opening credentials, contacting an API or touching Docker.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import ipaddress
import json
import re
import reprlib
import sys
import tomllib
import pathlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from provider_core.api_config import ApiConfigurationRejected, require_api_fields
from provider_core.bound_http_proxy import require_interface_name
from provider_core.delivery_client import DeliveryEndpoint, delivery_volume_name
from provider_core.delivery_store import MAX_BINDINGS, QueueLimits
from provider_core.deployment_errors import BindingDefinitionRejected
from provider_core.provider_proxy import ServicePolicy, ServiceRefused
from provider_core.startup_file import StartupFileRejected, read_startup_file
from provider_core.type_requirements import require_type
from provider_core.tools import require_positive_number
from provider_core.validation.upstream.docker_ids import is_full_network_id


def _fields(table, expected, section_name):
    require_type(
        table,
        dict,
        error_factory=lambda diagnostic: BindingDefinitionRejected(f"{section_name=}; " + diagnostic),
    )

    if set(table) != set(expected):
        actual_field_names = sorted(table)
        expected_field_names = sorted(expected)

        raise BindingDefinitionRejected(f"{section_name=}: {actual_field_names=} should match {expected_field_names=}")


def _absolute_path(path_text, path_field):
    require_type(
        path_text,
        str,
        error_factory=lambda diagnostic: BindingDefinitionRejected(f"{path_field=}; " + diagnostic),
    )

    if not (path_text):
        raise BindingDefinitionRejected(f"{path_field=}: {path_text=} should be an absolute path")

    if any((character in path_text) for character in ("\0", "\n", "\r", ",")):
        raise BindingDefinitionRejected(f"{path_field=}: {reprlib.repr(path_text)=} should contain no NUL, line breaks or commas")

    binding_path = pathlib.Path(path_text)

    if (binding_path.anchor != "/") or (".." in binding_path.parts) or (str(binding_path) != path_text) or (not binding_path.name):
        raise BindingDefinitionRejected(f"{path_field=}: {reprlib.repr(path_text)=} should be a normalized absolute path")

    return binding_path


@dataclass(frozen=True, slots=True, repr=False)
class DeploymentBinding:
    name: str
    project: str
    socket_group_id: int
    socket_path: pathlib.Path
    api: dict
    route: dict

    @property
    def ipc_volume(self):
        return delivery_volume_name(self.api["origin"], self.api["provider_ref"], namespace=self.project)

    @property
    def endpoint(self):
        return DeliveryEndpoint(
            self.api["origin"],
            self.api["provider_ref"],
            self.socket_path,
            socket_group_id=self.socket_group_id
        )


@dataclass(frozen=True, slots=True, repr=False)
class DeploymentBindings:
    project: str
    uid: int
    gid: int
    request_seconds: float
    retry_initial_seconds: float
    retry_max_seconds: float
    limits: dict
    bindings: tuple[DeploymentBinding, ...]

    def selected(self, name):
        for binding in self.bindings:
            if binding.name == name:
                return binding

        binding_names = [binding.name for binding in self.bindings]

        raise BindingDefinitionRejected(f"{reprlib.repr(name)=} ∉ {binding_names=}")


def _route(table, origin):
    require_type(
        table,
        dict,
        error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery route: " + diagnostic),
    )

    if not ("mode" in table):
        raise BindingDefinitionRejected(f"{reprlib.repr(set(table))=} should include 'mode'")

    mode = table["mode"]
    route_extra_fields = {
        "local": {"network_id", "addresses"},
        "mapped": {"addresses"},
        "external": {"resolver_file"},
    }

    require_type(mode, str, error_factory=BindingDefinitionRejected)

    if not (mode in route_extra_fields):
        raise BindingDefinitionRejected(f"{reprlib.repr(mode)=} ∉ {set(route_extra_fields)=}")

    required = {"mode", "interface", "log_level"} | route_extra_fields[mode]

    _fields(table, required, "route")

    require_type(table["interface"], str, error_factory=BindingDefinitionRejected)

    require_interface_name(table["interface"])

    if not (table["log_level"] in (expected_log_levels := ("INFO", "DEBUG", "WARNING"))):
        raise BindingDefinitionRejected(f"{reprlib.repr(table['log_level'])=} ∉ {expected_log_levels=}")

    if mode == "local":
        require_type(
            (table_network_id := table["network_id"]),
            str,
            error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit local delivery route: " + diagnostic),
        )

        if not is_full_network_id(table_network_id):
            raise BindingDefinitionRejected(f"{reprlib.repr(table_network_id)=} should be a full immutable Docker network ID")

    if mode == "external":
        _absolute_path(table["resolver_file"], "resolver file")
        addresses = []
    else:
        addresses = table["addresses"]

        require_type(
            addresses,
            list,
            error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery route: " + diagnostic),
        )

        for address in addresses:
            require_type(
                address,
                str,
                error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery route: " + diagnostic),
            )

    parsed = urlsplit(origin)

    # HTTPS owns port 443 when the canonical origin omits an explicit port.
    port = parsed.port

    if port is None:
        port = 443

    service_authority = f"{parsed.hostname}:{port}"  # machine-string: service policy parses this exact hostname and port authority
    policy = ServicePolicy.admit(mode, [service_authority], addresses)

    if not (parsed.hostname != "0.0.0.0"):
        raise BindingDefinitionRejected(f"{parsed.hostname=} should identify a specific API peer")

    for (_, peer_address) in policy.addresses:
        if not (not ipaddress.IPv4Address(peer_address).is_unspecified):
            raise BindingDefinitionRejected(f"{peer_address=} should identify a specific API peer")

    if mode == "local":
        for (_, peer_address) in policy.addresses:
            if not (not ipaddress.IPv4Address(peer_address).is_loopback):
                raise BindingDefinitionRejected(f"Loopback {peer_address=} identifies this proxy for {mode=}")

    return dict(table)


def _parse_deployment_bindings(raw):
    try:
        deployment_definition = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise BindingDefinitionRejected("Cannot read delivery bindings: require a UTF-8 TOML definition.") from cause

    _fields(deployment_definition, {"project", "uid", "gid", "request_seconds", "retry_initial_seconds", "retry_max_seconds", "limits", "bindings"}, "definition")
    (project, uid, gid) = (deployment_definition["project"], deployment_definition["uid"], deployment_definition["gid"])

    require_type(
        project,
        str,
        error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery bindings: " + diagnostic),
    )

    if not (re.fullmatch(project_pattern := r"[a-z0-9][a-z0-9_-]{0,62}", project) is not None):
        raise BindingDefinitionRejected(f"{reprlib.repr(project)=} should match {project_pattern=}")

    require_type(
        uid,
        int,
        error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery bindings: " + diagnostic),
    )

    (min_owner_id, max_owner_id) = (1, 4294967294)

    if not (min_owner_id <= uid <= max_owner_id):
        raise BindingDefinitionRejected(f"{uid=} should be between {min_owner_id=} and {max_owner_id=}")

    require_type(
        gid,
        int,
        error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery bindings: " + diagnostic),
    )

    if not (min_owner_id <= gid <= max_owner_id):
        raise BindingDefinitionRejected(f"{gid=} should be between {min_owner_id=} and {max_owner_id=}")

    require_positive_number(deployment_definition["request_seconds"])
    require_positive_number(deployment_definition["retry_initial_seconds"])
    require_positive_number(deployment_definition["retry_max_seconds"])

    if not (deployment_definition["retry_initial_seconds"] <= deployment_definition["retry_max_seconds"]):
        raise BindingDefinitionRejected(f"{deployment_definition['retry_initial_seconds']=} should be at most {deployment_definition['retry_max_seconds']=}")

    _fields(deployment_definition["limits"], {"provider_items", "provider_bytes", "total_items", "total_bytes"}, "limits")

    try:
        limits = QueueLimits(**deployment_definition["limits"])
    except ValueError as cause:
        # QueueLimits owns the item and byte relationships
        raise BindingDefinitionRejected("Cannot configure the delivery queue") from cause

    binding_rows = deployment_definition["bindings"]

    require_type(
        binding_rows,
        list,
        error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery bindings: " + diagnostic),
    )

    if not (1 <= len(binding_rows) <= MAX_BINDINGS):
        raise BindingDefinitionRejected(f"{len(binding_rows)=} should be positive and not exceed {MAX_BINDINGS=}")

    (bindings, binding_names, endpoint_identities) = ([], set(), set())

    for row in binding_rows:
        _fields(row, {"name", "socket_path", "api", "route"}, "binding")
        name = row["name"]

        require_type(
            name,
            str,
            error_factory=lambda diagnostic: BindingDefinitionRejected("Cannot admit delivery bindings: " + diagnostic),
        )

        if not (re.fullmatch(binding_name_pattern := r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", name) is not None):
            raise BindingDefinitionRejected(f"{reprlib.repr(name)=} should match {binding_name_pattern=}")

        if not (name not in binding_names):
            raise BindingDefinitionRejected(f"{name=} already identifies a binding")

        require_api_fields(row["api"])

        for key in ("credential_file", "ca_file"):
            if key in row["api"]:
                _absolute_path(row["api"][key], "API trust input")

        binding = DeploymentBinding(name, project, gid, _absolute_path(row["socket_path"], "provider socket"), dict(row["api"]), {})

        if not (binding.socket_path.parent != (filesystem_root := pathlib.Path("/"))):
            raise BindingDefinitionRejected(f"{binding.socket_path=} should have a parent below {filesystem_root=}")

        endpoint = binding.endpoint
        endpoint_identity = (endpoint.api_origin, endpoint.provider_ref)

        if not (endpoint_identity not in endpoint_identities):
            raise BindingDefinitionRejected(f"{reprlib.repr(endpoint_identity)=} already belongs to another binding")

        binding = DeploymentBinding(name, project, gid, binding.socket_path, binding.api, _route(row["route"], endpoint.api_origin))

        binding_names.add(name)
        endpoint_identities.add(endpoint_identity)
        bindings.append(binding)

    required_host_items = len(bindings) * limits.provider_items

    if not (required_host_items <= limits.total_items):
        raise BindingDefinitionRejected(f"{required_host_items=} should not exceed {limits.total_items=}")

    required_host_bytes = len(bindings) * limits.provider_bytes

    if not (required_host_bytes <= limits.total_bytes):
        raise BindingDefinitionRejected(f"{required_host_bytes=} should not exceed {limits.total_bytes=}")

    return DeploymentBindings(project, uid, gid, deployment_definition["request_seconds"], deployment_definition["retry_initial_seconds"], deployment_definition["retry_max_seconds"], dict(deployment_definition["limits"]), tuple(bindings))


def parse_deployment_bindings(raw):
    """Raise one safe refusal type, retaining reviewed helper/parser causes."""

    try:
        return _parse_deployment_bindings(raw)
    except BindingDefinitionRejected:
        raise
    except (ApiConfigurationRejected, ServiceRefused) as cause:
        raise BindingDefinitionRejected("Cannot configure delivery bindings") from cause
    except (ValueError, TypeError) as cause:
        raise BindingDefinitionRejected("Cannot configure delivery bindings") from cause


def load_deployment_bindings(path):
    try:
        raw = read_startup_file(path, maximum_bytes=65536)
    except StartupFileRejected as cause:
        raise BindingDefinitionRejected(f"Cannot load {reprlib.repr(path)=}") from cause

    return parse_deployment_bindings(raw)


def input_paths(bindings):
    """Name only the API trust and resolver files granted by selected bindings."""
    paths = set()

    for binding in bindings:
        paths.add(binding.api["credential_file"])

        if "ca_file" in binding.api:
            paths.add(binding.api["ca_file"])

        if binding.route["mode"] == "external":
            paths.add(binding.route["resolver_file"])

    return tuple(sorted(paths))


def _report_definition_failure(failure):
    # Native/parser nodes expose safe facts through the existing protected graph.
    from provider_core.diagnostics import failure_evidence

    print(
        json.dumps(
            {
                "operation": "inspect delivery binding",
                "failure": failure_evidence(
                    failure,
                    secrets=(),
                )
            },
            ensure_ascii=True
        ),
        file=sys.stderr
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("paths", "describe-binding"))
    parser.add_argument("--definition", type=pathlib.Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)

    selection.add_argument("--binding")
    selection.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if (args.action == "describe-binding") and args.all:
        parser.error("describe-binding requires one --binding name")

    try:
        definition = load_deployment_bindings(args.definition)

        if args.all:
            bindings = definition.bindings
        else:
            bindings = (definition.selected(args.binding),)

        if args.action == "describe-binding":
            binding = bindings[0]

            print(
                json.dumps(
                    {
                        "ipc_volume": binding.ipc_volume,
                        "socket_path": str(binding.socket_path),
                        "socket_group_id": binding.socket_group_id,
                        "input_paths": input_paths(bindings),
                    },
                    ensure_ascii=True
                )
            )
        else:
            for binding_input_path in input_paths(bindings):
                sys.stdout.buffer.write(binding_input_path.encode("utf-8") + b"\0")

        return 0
    # silent-defaults: allow-exception-outcome -- This executable reports safe retained evidence and refuses malformed private definitions
    except BindingDefinitionRejected as failure:
        _report_definition_failure(failure)

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
