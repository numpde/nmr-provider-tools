"""Admit names for the proxy's supported DNS query profile."""

from provider_core.container_guard import require_container

require_container()

import reprlib
import socket
import struct


class DNSResponseRejected(OSError):
    """A DNS reply cannot supply the requested IPv4 records."""


class DNSNameRejected(ValueError):
    """A DNS wire name cannot be decoded within the selected work cap."""


def normalize_dns_name(hostname: str) -> str:
    """Normalize a comparison name without changing name-admission timing."""

    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


# Preserve the acquisition proxy's admitted name profile from its donor.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L96-L298
def read_dns_labels(hostname: str) -> list[bytes]:
    """Return IDNA labels admitted by the existing 253-byte name profile.

    The byte limit includes the supplied trailing dot; the encoded labels omit it.
    Native IDNA errors propagate before the explicit name and label bounds.
    """

    labels = hostname.rstrip(".").encode("idna").split(b".")
    name_byte_count = len(hostname.encode("idna"))
    max_name_bytes = 253

    if not (name_byte_count <= max_name_bytes):
        raise ValueError(f"{name_byte_count=} should be at most {max_name_bytes=}")

    (min_label_bytes, max_label_bytes) = (1, 63)

    for dns_label in labels:
        label_byte_count = len(dns_label)

        if not (min_label_bytes <= label_byte_count <= max_label_bytes):
            raise ValueError(f"{label_byte_count=} should be between {min_label_bytes=} and {max_label_bytes=}")

    return labels


# silent-defaults: allow-contract -- Each top-level name starts at depth zero; pointer recursion advances it. The caller supplies its work cap.
def read_dns_name(
    message: bytes,
    offset: int,
    depth: int = 0,
    *,
    max_pointer_depth: int,
) -> tuple[str, int]:
    """Read lower-case ASCII text and the byte after its encoded representation.

    Compression pointers can reference the entire message. The depth cap bounds
    decoder work; it is supplied by the proxy rather than imposed by DNS itself.
    """

    if not (depth <= max_pointer_depth):
        raise DNSNameRejected(f"{depth=} should be at most {max_pointer_depth=}")

    labels: list[bytes] = []
    next_offset: int | None = None
    message_byte_count = len(message)

    while True:
        if not (offset < message_byte_count):
            raise DNSNameRejected(f"{offset=} should be below {message_byte_count=}")

        size = message[offset]

        if (size & 0xC0) == 0xC0:
            pointer_end = offset + 2

            if not (pointer_end <= message_byte_count):
                raise DNSNameRejected(f"{pointer_end=} should be at most {message_byte_count=}")

            pointer = ((size & 0x3F) << 8) | message[offset + 1]
            (suffix, _) = read_dns_name(message, pointer, depth + 1, max_pointer_depth=max_pointer_depth)

            if suffix:
                labels.extend(part.encode("ascii") for part in suffix.split("."))

            next_offset = next_offset or (offset + 2)

            break

        if not ((label_prefix := size & 0xC0) == 0):
            raise DNSNameRejected(f"{label_prefix=:#04x} should be zero")

        offset += 1

        if size == 0:
            next_offset = next_offset or offset

            break

        label_end = offset + size

        if not (label_end <= message_byte_count):
            raise DNSNameRejected(f"{label_end=} should be at most {message_byte_count=}")

        labels.append(message[offset:label_end])
        offset = label_end

    try:
        return (b".".join(labels).decode("ascii").lower(), next_offset)
    except UnicodeDecodeError as cause:
        label_byte = cause.object[cause.start]
        max_ascii_byte = 127

        raise DNSNameRejected(f"{label_byte=} should be at most {max_ascii_byte=}") from cause


def read_ipv4_response(
    response: bytes,
    *,
    hostname: str,
    transaction: int,
    max_pointer_depth: int,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read matching IN/A and IN/CNAME records from all declared sections.

    Preserve received A-record order and the last CNAME for each owner. Following
    aliases, selecting addresses and deciding whether to retry remain local policy.
    """

    response_byte_count = len(response)
    min_header_bytes = 12

    if not (min_header_bytes <= response_byte_count):
        raise DNSResponseRejected(f"{response_byte_count=} should be at least {min_header_bytes=}")

    (response_id, flags, question_count, answer_count, authority_count, additional_count) = struct.unpack("!HHHHHH", response[:12])

    if not (response_id == transaction):
        raise DNSResponseRejected(f"{response_id=} should match {transaction=}")

    if not (flags & 0x8000):
        raise DNSResponseRejected(f"{flags=:#06x} should mark a response")

    if not ((flags & 0x0200) == 0):
        raise DNSResponseRejected(f"{flags=:#06x} should not mark a truncated response")

    if not ((rcode := flags & 0x000F) == 0):
        raise DNSResponseRejected(f"{rcode=} should be zero")

    if not (question_count == 1):
        raise DNSResponseRejected(f"{question_count=} should be one")

    offset = 12
    (question_name, offset) = read_dns_name(response, offset, max_pointer_depth=max_pointer_depth)
    question_end = offset + 4

    if not (question_end <= response_byte_count):
        raise DNSResponseRejected(f"{question_end=} should be at most {response_byte_count=}")

    (question_type, question_class) = struct.unpack("!HH", response[offset:question_end])
    offset = question_end
    expected_name = normalize_dns_name(hostname)
    expected_question = (1, 1)

    if not (question_name == expected_name):
        # Compression can expand names beyond the packet size; keep previews bounded.
        raise DNSResponseRejected(f"{reprlib.repr(question_name)=} should match {reprlib.repr(expected_name)=}")

    if not ((question_type, question_class) == expected_question):
        raise DNSResponseRejected(f"{(question_type, question_class)=} should match {expected_question=}")

    addresses: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}

    for _ in range(answer_count + authority_count + additional_count):
        (owner, offset) = read_dns_name(response, offset, max_pointer_depth=max_pointer_depth)
        record_header_end = offset + 10

        if not (record_header_end <= response_byte_count):
            raise DNSResponseRejected(f"{record_header_end=} should be at most {response_byte_count=}")

        (record_type, record_class, _ttl, size) = struct.unpack("!HHIH", response[offset:record_header_end])
        offset = record_header_end
        record_end = offset + size

        if not (record_end <= response_byte_count):
            raise DNSResponseRejected(f"{record_end=} should be at most {response_byte_count=}")

        if (record_class == 1) and (record_type == 1) and (size == 4):
            # silent-defaults: allow-contract -- Owner buckets contain only received A records; no absent owner is invented.
            addresses.setdefault(owner, []).append(socket.inet_ntoa(response[offset:record_end]))
        elif (record_class == 1) and (record_type == 5):
            (aliases[owner], _) = read_dns_name(response, offset, max_pointer_depth=max_pointer_depth)

        offset = record_end

    return (addresses, aliases)
