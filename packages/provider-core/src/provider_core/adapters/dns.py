"""Construct DNS query names from admitted IDNA labels."""

from provider_core.container_guard import require_container

require_container()

from provider_core.validation.upstream.dns import read_dns_labels


def encode_dns_name(hostname: str) -> bytes:
    """Encode the supported name profile as length-prefixed DNS wire labels."""

    labels = read_dns_labels(hostname)

    return b"".join(bytes((len(label),)) + label for label in labels) + b"\0"
