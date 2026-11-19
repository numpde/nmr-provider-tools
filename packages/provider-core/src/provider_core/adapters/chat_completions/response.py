"""Translate admitted Chat completion facts into the carrier's retained failures."""

from provider_core.container_guard import require_container

require_container()

from provider_core.chat_errors import ChatFailed
from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.chat_completions.response import read_choice, require_completed_finish_reason


def read_completion(response, *, maximum_bytes: int) -> dict:
    """Return the original message or retain the original reply and rejection cause."""

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
