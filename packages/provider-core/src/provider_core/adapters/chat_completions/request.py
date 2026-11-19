"""Construct authenticated Chat HTTP requests after the caller admits their bytes."""

from provider_core.container_guard import require_container

require_container()

import httpx


def completion_request(*, url: str, api_key: str, content: bytes) -> httpx.Request:
    """Construct one request after the caller admits its encoded byte budget."""

    return httpx.Request(
        "POST",
        url,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        content=content,
    )
