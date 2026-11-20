"""Translate admitted Chat completion facts into the carrier's retained failures."""

from provider_core.container_guard import require_container

require_container()

from provider_core.chat_errors import ChatFailed
from provider_core.adapters.chat_completions.diagnostics import endpoint_error
from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.chat_completions.response import is_completion_success, read_choice, require_completed_finish_reason


def read_completion(response, *, maximum_bytes: int, error_detail_maximum_bytes: int, messages, api_key: str) -> dict:
    """Return the original message or retain the original reply and rejection cause."""

    if not is_completion_success(response.status):
        raise ChatFailed(
            "http_status",
            f"Cannot complete interpreter request: the model service returned HTTP {response.status=}; no tool was accepted.",
            response=response,
            endpoint_error=endpoint_error(response, messages, api_key, maximum_bytes=error_detail_maximum_bytes),
        )

    try:
        choice = read_choice(response.body, maximum_bytes=maximum_bytes)
        finish_reason = choice.get("finish_reason")

        if finish_reason == "length":
            raise ChatFailed(
                "completion_limit",
                "Cannot interpret the model reply: generation stopped at the service's completion limit; no tool was accepted.",
                response=response,
            )

        require_completed_finish_reason(finish_reason)
    except ResponseRejected as cause:
        raise ChatFailed(
            "invalid_response",
            "Cannot interpret the model reply: it is not one complete supported JSON completion; no tool was accepted.",
            cause=cause,
            response=response,
        ) from None

    return choice["message"]
