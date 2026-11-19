"""Read the resolver-file syntax consumed by the interface-bound proxy."""

from provider_core.container_guard import require_container

require_container()


def nameserver_tokens(resolver_text: str) -> list[str]:
    """Read address tokens from the supported two-field nameserver records."""

    return [
        fields[1]
        for line in resolver_text.splitlines()
        if (len(fields := line.split()) == 2) and (fields[0] == "nameserver")
    ]
