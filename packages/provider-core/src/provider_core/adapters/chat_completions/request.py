"""Encode the supported tools-first, non-streaming Chat Completions request.

This profile follows the existing Magnet carrier, rather than claiming a pinned
universal service schema. The caller selects the endpoint, model, context and
limits and owns whether a new billed request may be sent.
"""

from provider_core.container_guard import require_container

require_container()

import json

import httpx


def request_body(*, model: str, messages: list[dict], tools: list[dict], reasoning_effort: str | None) -> bytes:
    """Encode the caller's context without changing its tool or reasoning policy."""

    request = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "stream": False,
    }

    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort

    return json.dumps(request, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def completion_request(*, url: str, api_key: str, content: bytes) -> httpx.Request:
    """Construct one request after the caller admits its encoded byte budget."""

    return httpx.Request(
        "POST",
        url,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        content=content,
    )
