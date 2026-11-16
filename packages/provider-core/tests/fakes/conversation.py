"""Script model carriers and record owned endpoint cleanup without networking."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock


def interpreter_test_limits():
    """Give test conversations explicit budgets independent of deployment policy."""

    return {
        "end_model_visit_after_turns": 12,
        "end_model_visit_after_rejections": 3,
        "total_http_attempts_per_turn": 2,
        "connection_timeout_seconds": 10,
        "retain_history_up_to_bytes": 1048576,
        "retain_history_up_to_records": 48,
    }


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
