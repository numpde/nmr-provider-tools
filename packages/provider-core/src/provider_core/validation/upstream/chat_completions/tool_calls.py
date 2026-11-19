"""Admit carrier fields at the transcript owner's existing sequencing points.

The supported reasoning_content extension follows Magnet
600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf, openai_chat_interpreter.py:663-669.
This is a supported carrier profile, not a universal service schema. Identifier
bounds, replay exclusion, secret checks and correction permission remain local.
"""

from provider_core.container_guard import require_container

require_container()

import reprlib
from collections.abc import Iterator

from provider_core.conversation_errors import ConversationFailed


def require_assistant(message: object) -> None:
    """Admit the role before any optional field or tool-call validation."""

    if not (type(message) is (expected_type := dict)):
        raise ConversationFailed(f"{type(message)=} ≠ {expected_type=}")

    message_role = message.get("role")
    expected_role = "assistant"

    if not (message_role == expected_role):
        raise ConversationFailed(f"{reprlib.repr(message_role)=} should equal {expected_role=}")


def assistant_text_fields(message: dict) -> Iterator[tuple[str, str | None]]:
    """Yield each admitted field before inspecting the next one.

    The caller checks secrets between yields; eager validation would change
    which failure precedes a later malformed field.
    """

    for text_field in ("content", "reasoning_content"):
        if text_field in message:
            assistant_text = message[text_field]

            if (assistant_text is not None) and (not (type(assistant_text) is (expected_type := str))):
                raise ConversationFailed(f"{text_field=}; {type(assistant_text)=} ≠ {expected_type=}")

            yield text_field, assistant_text


def require_tool_calls(tool_calls: object) -> list:
    """Admit the list type after the caller considers a plain-text repair."""

    if not (type(tool_calls) is (expected_type := list)):
        raise ConversationFailed(f"{type(tool_calls)=} ≠ {expected_type=}")

    return tool_calls


def read_call_id(call: object) -> str:
    """Read an identifier before the owner applies grammar and replay policy."""

    if not (type(call) is (expected_type := dict)):
        raise ConversationFailed(f"{type(call)=} ≠ {expected_type=}")

    call_id = call.get("id")

    if not (type(call_id) is (expected_type := str)):
        raise ConversationFailed(f"{type(call_id)=} ≠ {expected_type=}")

    return call_id


def read_function_name(call: dict) -> str:
    """Read the function carrier after the owner admits the call identity."""

    call_type = call.get("type")
    expected_call_type = "function"

    if not (call_type == expected_call_type):
        raise ConversationFailed(f"{reprlib.repr(call_type)=} should equal {expected_call_type=}")

    function = call.get("function")

    if not (type(function) is (expected_type := dict)):
        raise ConversationFailed(f"{type(function)=} ≠ {expected_type=}")

    function_name = function.get("name")

    if not (type(function_name) is (expected_type := str)):
        raise ConversationFailed(f"{type(function_name)=} ≠ {expected_type=}")

    return function_name


def read_arguments_text(call: dict) -> str:
    """Read argument text after the owner admits the function name."""

    function_arguments = call["function"].get("arguments")

    if not (type(function_arguments) is (expected_type := str)):
        raise ConversationFailed(f"{type(function_arguments)=} ≠ {expected_type=}")

    return function_arguments
