"""Admit one assistant choice from the supported Chat Completions profile.

The SDK consumes one non-streaming choice. This supported subset follows the
existing Magnet carrier; it is not an exhaustive, pinned service schema.
Caller-selected byte budgets and model-visit decisions remain outside admission.
"""

from provider_core.container_guard import require_container

require_container()

import reprlib

from provider_core.response_json import ResponseRejected, read_document


# The supported completion profile uses HTTP 200 for successful replies
# repo-check: allow-module-constant
COMPLETION_SUCCESS_STATUS = 200


# Reviewed optional service fields; other response members are not diagnostics
# repo-check: allow-module-constant
ERROR_TEXT_FIELDS = ("message", "type", "code", "param")


def is_completion_success(http_status: int) -> bool:
    """Recognize the supported carrier's successful completion response status."""

    return http_status == COMPLETION_SUCCESS_STATUS


def error_fields(completion_error: object) -> dict[str, str] | None:
    """Select readable service fields, distinguishing a missing error object."""

    # silent-defaults: allow-contract -- Non-object JSON has no service error object.
    fields = completion_error.get("error") if type(completion_error) is dict else None

    if type(fields) is not dict:
        return None

    return {name: fields[name] for name in ERROR_TEXT_FIELDS if type(fields.get(name)) is str}


def read_choice(response_body: bytes, *, maximum_bytes: int) -> dict:
    """Admit the carrier shape before its finish reason is interpreted."""

    completion = read_document(response_body, maximum_bytes)

    if not (type(completion) is (expected_type := dict)):
        raise ResponseRejected(f"{type(completion)=} ≠ {expected_type=}")

    choices = completion.get("choices")

    if not (type(choices) is (expected_type := list)):
        raise ResponseRejected(f"{type(choices)=} ≠ {expected_type=}")

    choice_count = len(choices)
    expected_choice_count = 1

    if not (choice_count == expected_choice_count):
        raise ResponseRejected(f"{choice_count=} should equal {expected_choice_count=}")

    choice = choices[0]

    if not (type(choice) is (expected_type := dict)):
        raise ResponseRejected(f"{type(choice)=} ≠ {expected_type=}")

    assistant_message = choice.get("message")

    if not (type(assistant_message) is (expected_type := dict)):
        raise ResponseRejected(f"{type(assistant_message)=} ≠ {expected_type=}")

    message_role = assistant_message.get("role")
    expected_role = "assistant"

    if not (message_role == expected_role):
        raise ResponseRejected(f"{reprlib.repr(message_role)=} should equal {expected_role=}")

    return choice


def require_completed_finish_reason(finish_reason: object) -> None:
    """Admit the SDK's completed-reply subset after length exhaustion is classified."""

    expected_finish_reasons = ("stop", "tool_calls")

    if not (finish_reason in expected_finish_reasons):
        raise ResponseRejected(f"{reprlib.repr(finish_reason)=} ∉ {expected_finish_reasons=}")
