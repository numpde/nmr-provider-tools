"""Render function-tool and transcript carriers from locally admitted facts."""

from provider_core.container_guard import require_container

require_container()


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
