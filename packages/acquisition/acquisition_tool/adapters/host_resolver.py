"""Construct host resolver queries and nameserver records."""

from collections.abc import Iterable, Iterator


def networkmanager_dns_command(interface: str) -> list[str]:
    """Ask NetworkManager for the selected link's IPv4 DNS values."""

    return [
        "nmcli",
        "--get-values",  # Omit field labels
        "IP4.DNS",  # Select only the IPv4 resolver field
        "device",
        "show",
        interface,
    ]


def resolved_dns_command(interface: str) -> list[str]:
    """Ask systemd-resolved for the selected link's DNS server list."""

    return [
        "resolvectl",
        "dns",  # Read DNS server settings for the supplied link
        interface,
    ]


def nameserver_lines(servers: Iterable[str]) -> Iterator[str]:
    """Encode the selected servers as resolv.conf nameserver records."""

    return (
        f"nameserver {server}\n"  # machine-string: resolv.conf requires a bare resolver address after nameserver
        for server in servers
    )
