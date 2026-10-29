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
import math
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from provider_core.api_config import ApiConfigurationRejected, require_api_fields
from provider_core.bound_http_proxy import require_interface_name
from provider_core.delivery_client import DeliveryEndpoint, delivery_volume_name
from provider_core.delivery_store import MAX_BINDINGS, QueueLimits
from provider_core.deployment_errors import BindingDefinitionRejected
from provider_core.provider_proxy import ServicePolicy, ServiceRefused
from provider_core.startup_file import StartupFileRejected, read_startup_file


def _fields(table, expected, label):
    if type(table) is not dict or set(table) != set(expected):
        raise BindingDefinitionRejected(f"Cannot admit delivery bindings: {label} must contain exactly its documented fields.")


def _absolute_path(value, label):
    if not (type(value) is str and value and not any(character in value for character in ("\0", "\n", "\r", ","))):
        raise BindingDefinitionRejected(f"Cannot admit delivery bindings: {label} requires an absolute path without mount delimiters.")
    path = Path(value)
    if path.anchor != "/" or ".." in path.parts or str(path) != value or not path.name:
        raise BindingDefinitionRejected(f"Cannot admit delivery bindings: {label} must be an absolute normalized path.")
    return path


@dataclass(frozen=True, slots=True, repr=False)
class DeploymentBinding:
    name: str
    project: str
    socket_group_id: int
    socket_path: Path
    api: dict
    route: dict

    @property
    def ipc_volume(self):
        return delivery_volume_name(self.api["origin"], self.api["provider_ref"], namespace=self.project)

    @property
    def endpoint(self):
        return DeliveryEndpoint(self.api["origin"], self.api["provider_ref"], self.socket_path,
                                socket_group_id=self.socket_group_id)


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
        raise BindingDefinitionRejected("Cannot select delivery binding: the selected name is absent from the definition.")


def _route(table, origin):
    if type(table) is not dict or "mode" not in table:
        raise BindingDefinitionRejected("Cannot admit delivery route: choose local, mapped or external mode explicitly.")
    mode = table["mode"]
    required = {"mode", "interface", "log_level"}
    if mode == "local":
        required |= {"network_id", "addresses"}
    elif mode == "mapped":
        required |= {"addresses"}
    elif mode == "external":
        required |= {"resolver_file"}
    else:
        raise BindingDefinitionRejected("Cannot admit delivery route: choose local, mapped or external mode explicitly.")
    _fields(table, required, "route")
    if type(table["interface"]) is not str:
        raise BindingDefinitionRejected("Cannot admit delivery route: interface must be an explicit Linux interface name.")
    require_interface_name(table["interface"])
    if table["log_level"] not in ("INFO", "DEBUG", "WARNING"):
        raise BindingDefinitionRejected("Cannot admit delivery route: log_level must be INFO, DEBUG or WARNING.")
    if mode == "local" and (type(table["network_id"]) is not str or re.fullmatch(r"[0-9a-f]{64}", table["network_id"]) is None):
        raise BindingDefinitionRejected("Cannot admit local delivery route: network_id must be the full immutable Docker network ID.")
    if mode == "external":
        _absolute_path(table["resolver_file"], "resolver file")
        addresses = []
    else:
        addresses = table["addresses"]
        if type(addresses) is not list or any(type(value) is not str for value in addresses):
            raise BindingDefinitionRejected("Cannot admit delivery route: addresses must be explicit hostname=IPv4 strings.")
    parsed = urlsplit(origin)
    # HTTPS owns port 443 when the canonical origin omits an explicit port.
    port = parsed.port
    if port is None:
        port = 443
    policy = ServicePolicy.admit(mode, [f"{parsed.hostname}:{port}"], addresses)
    if parsed.hostname == "0.0.0.0" or any(ipaddress.IPv4Address(address).is_unspecified for _, address in policy.addresses):
        raise BindingDefinitionRejected("Cannot admit API route: an unspecified address is not an API destination; select the intended peer address.")
    if mode == "local" and any(ipaddress.IPv4Address(address).is_loopback for _, address in policy.addresses):
        raise BindingDefinitionRejected("Cannot admit local API route: loopback belongs to this proxy; select the API peer on the declared network or mapped routing for host loopback.")
    return dict(table)


def _parse_deployment_bindings(raw):
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise BindingDefinitionRejected("Cannot read delivery bindings: require a UTF-8 TOML definition.") from cause
    _fields(document, {"project", "uid", "gid", "request_seconds", "retry_initial_seconds", "retry_max_seconds", "limits", "bindings"}, "definition")
    project, uid, gid = document["project"], document["uid"], document["gid"]
    if not (type(project) is str and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project) is not None):
        raise BindingDefinitionRejected("Cannot admit delivery bindings: project requires a lowercase Compose name of up to 63 characters.")
    if not all(type(value) is int and 1 <= value <= 4294967294 for value in (uid, gid)):
        raise BindingDefinitionRejected("Cannot admit delivery bindings: uid and gid must be explicit nonzero Linux identities.")
    for key in ("request_seconds", "retry_initial_seconds", "retry_max_seconds"):
        value = document[key]
        if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
            raise BindingDefinitionRejected(f"Cannot admit delivery bindings: {key} requires finite positive seconds.")
    if not document["retry_initial_seconds"] <= document["retry_max_seconds"]:
        raise BindingDefinitionRejected("Cannot admit delivery bindings: initial retry delay must not exceed maximum delay.")
    _fields(document["limits"], {"provider_items", "provider_bytes", "total_items", "total_bytes"}, "limits")
    try:
        limits = QueueLimits(**document["limits"])
    except ValueError as cause:
        # QueueLimits owns these fixed, input-free refusal messages.
        raise BindingDefinitionRejected(str(cause)) from cause
    rows = document["bindings"]
    if not (type(rows) is list and 1 <= len(rows) <= MAX_BINDINGS):
        raise BindingDefinitionRejected("Cannot admit delivery bindings: require one through sixteen explicit bindings.")
    bindings, names, identities = [], set(), set()
    for row in rows:
        _fields(row, {"name", "socket_path", "api", "route"}, "binding")
        name = row["name"]
        if not (type(name) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", name) is not None and name not in names):
            raise BindingDefinitionRejected("Cannot admit delivery bindings: names must be unique ASCII identifiers of up to 63 characters.")
        require_api_fields(row["api"])
        for key in ("credential_file", "ca_file"):
            if key in row["api"]:
                _absolute_path(row["api"][key], "API trust input")
        binding = DeploymentBinding(name, project, gid, _absolute_path(row["socket_path"], "provider socket"), dict(row["api"]), {})
        if binding.socket_path.parent == Path("/"):
            raise BindingDefinitionRejected("Cannot admit provider socket: select a socket leaf inside its own directory below the filesystem root.")
        endpoint = binding.endpoint
        identity = endpoint.api_origin, endpoint.provider_ref
        if identity in identities:
            raise BindingDefinitionRejected("Cannot admit delivery bindings: two bindings claim the same API/provider identity.")
        binding = DeploymentBinding(name, project, gid, binding.socket_path, binding.api, _route(row["route"], endpoint.api_origin))
        names.add(name)
        identities.add(identity)
        bindings.append(binding)
    if not (len(bindings) * limits.provider_items <= limits.total_items and len(bindings) * limits.provider_bytes <= limits.total_bytes):
        raise BindingDefinitionRejected("Cannot admit delivery bindings: host queue limits must cover all provider shares.")
    return DeploymentBindings(project, uid, gid, document["request_seconds"], document["retry_initial_seconds"], document["retry_max_seconds"], dict(document["limits"]), tuple(bindings))


def parse_deployment_bindings(raw):
    """Raise one safe refusal type, retaining reviewed helper/parser causes."""
    try:
        return _parse_deployment_bindings(raw)
    except BindingDefinitionRejected:
        raise
    except (ApiConfigurationRejected, ServiceRefused) as cause:
        raise BindingDefinitionRejected(str(cause)) from cause
    except (ValueError, TypeError) as cause:
        raise BindingDefinitionRejected("Cannot admit delivery bindings: API identity or route values do not satisfy the documented types and bounds.") from cause


def load_deployment_bindings(path):
    try:
        raw = read_startup_file(path, maximum_bytes=65536, label="delivery binding definition")
    except StartupFileRejected as cause:
        raise BindingDefinitionRejected(str(cause)) from cause
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

    print(json.dumps({"operation": "inspect delivery binding", "failure": failure_evidence(
        failure, secrets=(),
    )}, ensure_ascii=True), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("paths", "describe-binding"))
    parser.add_argument("--definition", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--binding")
    selection.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.action == "describe-binding" and args.all:
        parser.error("describe-binding requires one --binding name")
    try:
        definition = load_deployment_bindings(args.definition)
        if args.all:
            bindings = definition.bindings
        else:
            bindings = (definition.selected(args.binding),)
        if args.action == "describe-binding":
            binding = bindings[0]
            print(json.dumps({
                "ipc_volume": binding.ipc_volume,
                "socket_path": str(binding.socket_path),
                "socket_group_id": binding.socket_group_id,
                "input_paths": input_paths(bindings),
            }, ensure_ascii=True))
        else:
            for path in input_paths(bindings):
                sys.stdout.buffer.write(path.encode("utf-8") + b"\0")
        return 0
    # silent-defaults: allow-exception-outcome -- This executable reports safe retained evidence and refuses malformed private definitions
    except BindingDefinitionRejected as failure:
        _report_definition_failure(failure)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
