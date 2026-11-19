"""Wrap locally owned parameter schemas in the Chat function-tool carrier."""

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
