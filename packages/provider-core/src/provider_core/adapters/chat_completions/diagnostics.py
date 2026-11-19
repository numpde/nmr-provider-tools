"""Interpret optional service diagnostics without owning their eventual display."""

from provider_core.container_guard import require_container

require_container()

import json
from collections.abc import Iterator

from provider_core.diagnostic_text import without_echoes
from provider_core.response_json import ResponseRejected, read_document
from provider_core.validation.upstream.chat_completions.response import ERROR_TEXT_FIELDS, error_fields


def _request_echoes(messages, key):
    texts = [key]

    for message in messages:
        if type(message) is not dict:
            continue

        texts.append(message.get("content"))
        calls = message.get("tool_calls")

        if type(calls) is list:
            for call in calls:
                if (type(call) is dict) and (type(call.get("function")) is dict):
                    texts.append(call["function"].get("arguments"))

    # repo-check: allow-tuple-generator -- Capture spellings once for consistent removal across the service fields.
    return tuple(
        spelling for text in texts if (type(text) is str) and text
        for spelling in (text, json.dumps(text, ensure_ascii=True)[1:-1])
    )


def endpoint_error(response, messages, key: str, *, maximum_bytes: int) -> dict[str, str]:
    """Project optional fields while retaining the original response elsewhere.

    This ports SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/chat.py:211-288. Text remains unverified;
    paraphrased input is not detected. Consumers own final redaction and limits.
    """

    try:
        completion_error = read_document(response.body, maximum_bytes)
    # silent-defaults: allow-exception-outcome -- Unreadable optional details never replace the HTTP refusal.
    except ResponseRejected:
        return {"detail_unavailable": f"The model service error detail was not complete readable JSON within {maximum_bytes=} bytes."}

    fields = error_fields(completion_error)

    if fields is None:
        return {"detail_unavailable": "The model service response contained no readable error object."}

    echoes = _request_echoes(messages, key)
    result = {name: without_echoes(text, echoes) for (name, text) in fields.items()}

    # silent-defaults: allow-contract -- Empty readable fields report unavailable detail without rendering the whole response.
    return result or {"detail_unavailable": "The model service response contained no readable error fields."}


def endpoint_error_fields(endpoint_error: dict) -> Iterator[tuple[str, str]]:
    """Expose only selected service text and the adapter's unavailable-detail marker."""

    for name in (*ERROR_TEXT_FIELDS, "detail_unavailable"):
        diagnostic_text = endpoint_error.get(name)

        if type(diagnostic_text) is str:
            yield (name, diagnostic_text)
