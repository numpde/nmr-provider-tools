"""Read HTTP content-coding order against the caller's supported decoder profile."""

from provider_core.container_guard import require_container

require_container()

from collections.abc import Collection


def read_content_codings(
    headers: tuple[tuple[bytes, bytes], ...],
    *,
    supported_codings: Collection[bytes],
    max_codings: int,
) -> tuple[bytes, ...] | None:
    """Return decode order, or no admitted stack for an unsupported declaration.

    Absence selects identity. The caller supplies its coding vocabulary and
    work cap and retains the rejection type; neither is a universal HTTP limit.
    """

    codings = []

    for name, header_value in headers:
        if name.lower() == b"content-encoding":
            for coding in header_value.split(b",", max_codings):
                coding = coding.strip(b" \t").lower()

                if (len(codings) == max_codings) or (coding not in supported_codings):
                    return None

                codings.append(coding)

    # silent-defaults: allow-contract -- An absent Content-Encoding header denotes identity encoding.
    return tuple(reversed(codings)) if codings else (b"identity",)
