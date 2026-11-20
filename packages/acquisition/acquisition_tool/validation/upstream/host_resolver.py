"""Read resolver tokens from the host tool and file formats used by acquisition."""


def networkmanager_dns_tokens(output: str) -> list[str]:
    """Read the value-only output selected by nmcli --get-values."""

    # https://networkmanager.dev/docs/api/latest/nmcli.html
    return output.split()


def resolved_dns_tokens(output: str) -> list[str]:
    """Read the possibly wrapped list after the resolvectl link label."""

    # https://github.com/systemd/systemd/blob/v252/src/resolve/resolvectl.c#L1281-L1304
    return output.partition(":")[2].split()


def nameserver_tokens(resolver_text: str) -> list[str]:
    """Read the address tokens from two-field resolv.conf nameserver lines."""

    # https://manpages.debian.org/bookworm/manpages/resolv.conf.5.en.html
    return [
        fields[1]
        for line in resolver_text.splitlines()
        if (len(fields := line.split()) == 2) and (fields[0] == "nameserver")
    ]
