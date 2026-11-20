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
from provider_core.type_requirements import require_type


def require_assistant(message: object) -> None:
    """Admit the role before any optional field or tool-call validation."""

    require_type(message, dict, error_factory=ConversationFailed)

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

            if assistant_text is not None:
                require_type(
                    assistant_text,
                    str,
                    error_factory=lambda diagnostic: ConversationFailed(f"{text_field=}; " + diagnostic),
                )

            yield (text_field, assistant_text)


def require_tool_calls(tool_calls: object) -> list:
    """Admit the list type after the caller considers a plain-text repair."""

    require_type(tool_calls, list, error_factory=ConversationFailed)

    return tool_calls


def read_call_id(call: object) -> str:
    """Read an identifier before the owner applies grammar and replay policy."""

    require_type(call, dict, error_factory=ConversationFailed)

    call_id = call.get("id")

    require_type(call_id, str, error_factory=ConversationFailed)

    return call_id


def read_function_name(call: dict) -> str:
    """Read the function carrier after the owner admits the call identity."""

    call_type = call.get("type")
    expected_call_type = "function"

    if not (call_type == expected_call_type):
        raise ConversationFailed(f"{reprlib.repr(call_type)=} should equal {expected_call_type=}")

    function = call.get("function")

    require_type(function, dict, error_factory=ConversationFailed)

    function_name = function.get("name")

    require_type(function_name, str, error_factory=ConversationFailed)

    return function_name


def read_arguments_text(call: dict) -> str:
    """Read argument text after the owner admits the function name."""

    function_arguments = call["function"].get("arguments")

    require_type(function_arguments, str, error_factory=ConversationFailed)

    return function_arguments
