"""Render function-tool and transcript carriers from locally admitted facts."""

from provider_core.container_guard import require_container

require_container()

import json
from typing import Any

from provider_core.response_json import read_document


def function_tool(*, name: str, description: str, parameters: dict) -> dict:
    """Advertise a tool without deriving its schema or granting dispatch permission."""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def tool_message(call_id: str, content: str) -> dict:
    """Link already admitted feedback to its original function call."""

    return {"role": "tool", "tool_call_id": call_id, "content": content}


def user_message(content: str) -> dict:
    """Frame the caller's repair instruction without choosing when to append it."""

    return {"role": "user", "content": content}


def assistant_message() -> dict:
    """Start the supported assistant carrier before admitted fields are attached."""

    return {"role": "assistant"}


def function_call(*, call_id: str, name: str, arguments: str) -> dict:
    """Frame admitted call facts without changing their identity or argument text."""

    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def message_tool_calls(message: dict) -> Any:
    """Read the optional carrier before the caller chooses its correction policy."""

    return message.get("tool_calls")


def attach_tool_calls(message: dict, calls: list[dict]) -> None:
    """Attach only fully admitted calls to the caller-owned assistant message."""

    message["tool_calls"] = calls


def call_id(call: dict) -> str:
    """Read an already admitted call's correlation identity."""

    return call["id"]


def call_name(call: dict) -> str:
    """Read an already admitted function name without selecting a local handler."""

    return call["function"]["name"]


def call_arguments(call: dict) -> str:
    """Read already admitted argument text without decoding or dispatching it."""

    return call["function"]["arguments"]


def replace_call_arguments(call: dict, arguments: str) -> None:
    """Keep the same call while the owner canonicalizes or erases its arguments."""

    call["function"]["arguments"] = arguments


def decode_arguments(argument_bytes: bytes) -> tuple[object, str]:
    """Read a JSON argument value and its canonical wire spelling."""

    arguments = read_document(argument_bytes, len(argument_bytes))
    canonical_arguments_text = json.dumps(arguments, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return arguments, canonical_arguments_text
