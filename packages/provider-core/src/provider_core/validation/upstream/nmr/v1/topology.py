"""Admit the routed API environment independently of credential profiles.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/edge_topology.py::EdgeTopology. Credential profile 'run' is not topology
'web'; configured expectations have no implicit default.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.type_requirements import require_type


def require_api_topology(expected_topology):
    """Admit and return one explicitly selected routed environment."""

    require_type(expected_topology, str)

    expected_topologies = ("dev-local", "dev", "web")

    if not (expected_topology in expected_topologies):
        raise ValueError(f"{expected_topology=} should be one of {expected_topologies=}")

    return expected_topology


def edge_topology_matches(headers: tuple[tuple[bytes, bytes], ...], expected_topology: str) -> bool:
    """Accept an absent edge marker or one matching the selected environment.

    Maintenance proxies may omit API headers. This does not admit their bodies
    as API replies; an explicit mismatch or repeated marker still refuses.
    """

    markers = [header_value for (header_name, header_value) in headers if header_name.lower() == b"nmr-api-topology"]

    return markers in ([], [expected_topology.encode("ascii")])
