"""Read the NMR API's bounded server-owned request correlation identifier.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/request_ids.py. A received identifier establishes no operation effect.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.validation.upstream.http import read_visible_ascii_header


def read_request_id(headers: tuple[tuple[bytes, bytes], ...]) -> str | None:
    """Return one admitted identifier; ambiguous or malformed evidence stays absent."""

    max_request_id_bytes = 128

    return read_visible_ascii_header(headers, header_name=b"x-request-id", max_value_bytes=max_request_id_bytes)
