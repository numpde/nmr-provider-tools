"""Capture configured host resolvers for one interface-bound acquisition scope."""

import argparse
import ipaddress
import os
import pathlib
import socket
import subprocess
import sys

if __name__ == "__main__":
    # Add the selected bundle root omitted by isolated Python
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from acquisition_tool.adapters import host_resolver as resolver_adapter
from acquisition_tool.validation.upstream import host_resolver as resolver_output


class ResolverCaptureFailed(OSError):
    """A safe explanation of why the scope has no usable resolver capture."""


def _addresses(resolver_tokens):
    servers = []

    for resolver_token in resolver_tokens:
        try:
            address = ipaddress.IPv4Address(resolver_token)
        except ipaddress.AddressValueError:
            continue

        if (not address.is_loopback) and (str(address) not in servers):
            servers.append(str(address))

    return tuple(servers)


def _manager_output(command):
    try:
        manager_command = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=3,
            check=False
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired as cause:
        raise ResolverCaptureFailed(
            "Cannot capture acquisition DNS: the host network manager did not answer within three seconds."
        ) from cause

    # An absent service or unmanaged link supplies no configuration.
    # Consult the other configured source, never an invented public resolver.
    if manager_command.returncode != 0:
        return ""

    return manager_command.stdout


def capture_dns_servers(interface):
    """Prefer link DNS; otherwise retain configured global DNS, still route-bound.

    Capture occurs once before online work. A usable link set is never extended
    with global servers, and query failures never change the captured source.
    Network changes take effect in the next acquisition scope.
    """

    if interface != "system":
        socket.if_nametoindex(interface)
        output = _manager_output(resolver_adapter.networkmanager_dns_command(interface))
        servers = _addresses(resolver_output.networkmanager_dns_tokens(output))

        if servers:
            return servers

        output = _manager_output(resolver_adapter.resolved_dns_command(interface))
        servers = _addresses(resolver_output.resolved_dns_tokens(output))

        if servers:
            return servers

    for resolver_file in (pathlib.Path("/run/systemd/resolve/resolv.conf"), pathlib.Path("/etc/resolv.conf")):
        try:
            resolver_text = resolver_file.read_text(encoding="ascii")
        except OSError:
            continue

        servers = _addresses(resolver_output.nameserver_tokens(resolver_text))

        if servers:
            return servers

    raise ResolverCaptureFailed("Cannot capture acquisition DNS: configure an IPv4 resolver for the selected interface or host.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--interface", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    try:
        servers = capture_dns_servers(args.interface)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.writelines(resolver_adapter.nameserver_lines(servers))
    except ResolverCaptureFailed as error:
        parser.exit(2, f"{error=!s}\n")
    except (OSError, UnicodeError) as error:
        parser.exit(
            2,
            f"Cannot prepare acquisition resolver capture: {error=!s}\n",
        )


if __name__ == "__main__":
    main()
