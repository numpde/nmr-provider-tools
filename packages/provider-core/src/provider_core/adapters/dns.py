"""Construct DNS query names from admitted IDNA labels."""

from provider_core.container_guard import require_container

require_container()

import struct

from provider_core.validation.upstream.dns import read_dns_labels


def encode_dns_name(hostname: str) -> bytes:
    """Encode the supported name profile as length-prefixed DNS wire labels."""

    labels = read_dns_labels(hostname)

    return b"".join(bytes((len(label),)) + label for label in labels) + b"\0"


def ipv4_query(hostname: str, transaction: int) -> bytes:
    """Build one recursive IN/A question with the caller's transaction ID."""

    question = encode_dns_name(hostname) + struct.pack("!HH", 1, 1)

    return struct.pack("!HHHHHH", transaction, 0x0100, 1, 0, 0, 0) + question
