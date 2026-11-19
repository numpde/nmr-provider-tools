"""Admit names for the proxy's supported DNS query profile."""

from provider_core.container_guard import require_container

require_container()


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

    min_label_bytes, max_label_bytes = 1, 63

    for label in labels:
        label_byte_count = len(label)

        if not (min_label_bytes <= label_byte_count <= max_label_bytes):
            raise ValueError(f"{label_byte_count=} should be between {min_label_bytes=} and {max_label_bytes=}")

    return labels
