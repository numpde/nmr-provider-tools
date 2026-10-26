"""Script model carriers and record owned endpoint cleanup without networking."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock


def call(identifier, name, arguments):
    return {
        "role": "assistant",
        "reasoning_content": "fixture reasoning",
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def endpoint(identifier, *replies):
    return SimpleNamespace(
        identity=json.dumps(
            {
                "configuration_id": identifier,
                "model": "fixture",
                "configuration_sha256": "a" * 64,
            }
        ).encode(),
        complete=AsyncMock(side_effect=replies),
        retire=AsyncMock(),
        retirement_confirmed=True,
    )


def recording_endpoint(identifier, *replies):
    model = endpoint(identifier, *replies)
    original = model.complete
    model.requests = []

    async def complete(messages, tools):
        model.requests.append(deepcopy(messages))
        return await original(messages, tools)

    model.complete = AsyncMock(side_effect=complete)
    return model
