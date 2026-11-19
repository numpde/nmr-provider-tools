"""Encode the supported Chat profile without importing an HTTP client.

Transcript and budget owners need these bytes with native-only model carriers.
The caller selects the model, context and capacity; this codec chooses no visit
or retry and supplies no universal service limit.
"""

from provider_core.container_guard import require_container

require_container()

import json


def _json_bytes(request: dict) -> bytes:
    return json.dumps(request, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def context_byte_count(*, messages: list[dict], tools: list[dict]) -> int:
    """Measure the same context framing used to reserve request-body capacity."""

    return len(_json_bytes({"messages": messages, "tools": tools}))


def request_body(*, model: str, messages: list[dict], tools: list[dict], reasoning_effort: str | None) -> bytes:
    """Encode the caller's context in the supported non-streaming tools-first profile."""

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

    return _json_bytes(request)
