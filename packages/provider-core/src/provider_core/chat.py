"""Carry Chat Completions tool turns over the bounded HTTPS/retirement owner.

Transport/config separation follows Magnet 600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf,
magnet_provider/openai_chat_interpreter.py; see this package's README for sources
and adaptation rationale. Composition grants endpoint facts, TLS and dedicated I/O.
The consumer owns tool dispatch, corrections, fallback and conversation work.
This adapter sends once: a lost reply does not authorize another billed request.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

import httpx

from provider_core.response_json import ResponseRejected, read_document
from provider_core.transport import ExchangeFailed


def _json_bytes(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatEndpointSpec:
    """An explicit complete endpoint URL, with no ambient configuration lookup."""

    configuration_id: str
    model: str
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    reasoning_effort: str | None

    def __post_init__(self):
        if type(self.configuration_id) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.configuration_id) is None:
            raise ValueError("Cannot configure interpreter endpoint: configuration_id must be a bounded ASCII identifier.")
        if (
            not (type(self.model) is str and self.model.strip() and self.model.isprintable() and len(self.model.encode('utf-8')) <= 512)
        ):
            raise ValueError(
                "Cannot configure interpreter endpoint: model must be nonblank printable UTF-8 text within 512 bytes."
            )
        if type(self.api_key) is not str or not self.api_key or any(not 32 < ord(char) < 127 for char in self.api_key):
            raise ValueError("Cannot configure interpreter endpoint: an explicit header-safe API key is required.")
        if self.reasoning_effort is not None and (
            type(self.reasoning_effort) is not str
            or not self.reasoning_effort
            or not self.reasoning_effort.isascii()
            or not self.reasoning_effort.isalnum()
        ):
            raise ValueError("Cannot configure interpreter endpoint: reasoning_effort must be an explicit ASCII word or None.")
        if type(self.url) is not str or any(not 32 < ord(char) < 127 for char in self.url):
            raise ValueError("Cannot configure interpreter endpoint: URL must be canonical ASCII HTTPS text.")
        try:
            url = httpx.URL(self.url)
        except httpx.InvalidURL:
            raise ValueError("Cannot configure interpreter endpoint: URL is not valid HTTPS.") from None
        if url.scheme != "https" or not url.host or url.userinfo or "?" in self.url or "#" in self.url or str(url) != self.url:
            raise ValueError(
                "Cannot configure interpreter endpoint: use a canonical HTTPS URL without credentials, query or fragment."
            )


class ChatFailed(RuntimeError):
    """Safe operation text; original HTTP response and causes stay private."""

    def __init__(self, reason, message, *, cause=None, response=None, endpoint_error=None):
        super().__init__(message)
        self.reason = reason
        self.cause = cause
        self.response = response
        self.endpoint_error = endpoint_error
        self.interruption_cause = None


def _request_echoes(messages, key):
    texts = [key]
    for message in messages:
        if type(message) is not dict:
            continue
        texts.append(message.get("content"))
        calls = message.get("tool_calls")
        if type(calls) is list:
            for call in calls:
                if type(call) is dict and type(call.get("function")) is dict:
                    texts.append(call["function"].get("arguments"))
    # repo-check: allow-tuple-generator -- Capture fixed secret spellings once for redaction across the asynchronous exchange.
    return tuple(spelling for text in texts if type(text) is str and text
                 for spelling in (text, json.dumps(text, ensure_ascii=True)[1:-1]))


def _without_echoes(value, echoes):
    # SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    # src/secs_inference/provider/chat.py::_diagnostic_text: merge original
    # matches so replacing a key cannot expose a larger overlapping prompt echo.
    spans = []
    for echo in echoes:
        start = value.find(echo)
        while start >= 0:
            spans.append((start, start + len(echo)))
            start = value.find(echo, start + 1)
    parts, end = [], 0
    for start, stop in sorted(spans):
        if start >= end:
            parts.extend((value[end:start], "[redacted]"))
        end = max(end, stop)
    parts.append(value[end:])
    # silent-defaults: allow-contract -- Replace nonprinting diagnostic characters with spaces while retaining readable redacted structure.
    return " ".join("".join(char if char.isprintable() else " " for char in "".join(parts)).split())


def _endpoint_error(response, messages, key):
    """Project optional error fields, never raw bodies or complete prompt echoes.

    This ports SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/chat.py:211-288. Text remains unverified/protected;
    paraphrased input is not detected. Consumers redact all configured secrets
    before display limits; truncating here could expose another key's prefix.
    """
    try:
        document = read_document(response.body, 8192)
    # silent-defaults: allow-exception-outcome -- Unreadable optional service detail is explicitly unavailable and never replaces the HTTP refusal
    except ResponseRejected:
        return {"detail_unavailable": "The model service error detail was not complete readable JSON within 8192 bytes."}
    # silent-defaults: allow-contract -- A non-object response supplies no usable error object.
    fields = document.get("error") if type(document) is dict else None
    if type(fields) is not dict:
        return {"detail_unavailable": "The model service response contained no readable error object."}
    echoes = _request_echoes(messages, key)
    result = {name: _without_echoes(fields[name], echoes)
              for name in ("message", "type", "code", "param") if type(fields.get(name)) is str}
    # silent-defaults: allow-contract -- An empty reviewed diagnostic selection reports detail unavailable without exposing the response body.
    return result or {"detail_unavailable": "The model service response contained no readable error fields."}


def require_chat_limits(*, request_bytes, response_bytes):
    """Admit carrier limits before startup gives it a transport."""
    if any(type(value) is not int or value <= 0 for value in (request_bytes, response_bytes)):
        raise ValueError("Cannot configure interpreter carrier: request and response byte limits must be positive integers.")


class ChatEndpoint:
    """One model carrier with caller-granted transport and explicit byte limits.

    The owner must call retire(), including after cancellation or failed exchange.
    retirement_confirmed must be true before the chooser can switch endpoints.
    No API/Upload credentials, scientific arrays or filesystem access are granted.
    """

    def __init__(self, spec, *, transport, request_bytes, response_bytes):
        if type(spec) is not ChatEndpointSpec:
            raise TypeError("Cannot configure interpreter carrier: explicit ChatEndpointSpec is required.")
        require_chat_limits(request_bytes=request_bytes, response_bytes=response_bytes)
        self.spec, self._transport = spec, transport
        self._request_bytes, self._response_bytes = request_bytes, response_bytes

    @property
    def identity(self):
        """Project configured facts without key/URL text or authentication identity."""
        configuration = {
            "configuration_id": self.spec.configuration_id,
            "model": self.spec.model,
            "url": self.spec.url,
            "reasoning_effort": self.spec.reasoning_effort,
            "request_bytes": self._request_bytes,
            "response_bytes": self._response_bytes,
        }
        return _json_bytes(
            {
                "configuration_id": self.spec.configuration_id.replace(self.spec.api_key, "[redacted]"),
                "model": self.spec.model.replace(self.spec.api_key, "[redacted]"),
                "configuration_sha256": hashlib.sha256(_json_bytes(configuration)).hexdigest(),
            }
        )

    @property
    def retirement_confirmed(self):
        return self._transport.retirement_confirmed

    async def retire(self):
        try:
            await self._transport.retire(owner=self)
        except ExchangeFailed as cause:
            raise ChatFailed(
                "retirement_unconfirmed",
                "Cannot retire interpreter I/O: preserve the transport and its cleanup evidence.",
                cause=cause,
            ) from None

    async def complete(self, messages, tools):
        """Send one bounded turn; return a complete assistant carrier after cleanup."""
        request = {
            "model": self.spec.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "stream": False,
        }
        if self.spec.reasoning_effort is not None:
            request["reasoning_effort"] = self.spec.reasoning_effort
        content = _json_bytes(request)
        if len(content) > self._request_bytes:
            raise ChatFailed(
                "request_limit",
                f"Cannot send interpreter request: its {len(content)} bytes exceed the {self._request_bytes}-byte limit; this turn was not sent.",
            )
        wire = httpx.Request(
            "POST",
            self.spec.url,
            headers={"Authorization": "Bearer " + self.spec.api_key, "Content-Type": "application/json"},
            content=content,
        )
        try:
            response = await self._transport.exchange(wire, maximum_bytes=self._response_bytes, owner=self)
        except asyncio.CancelledError as interruption:
            failure = ChatFailed(
                "request_cancelled",
                "Interpretation was interrupted; the model request may have been processed, but no tool was accepted. No resend was attempted.",
                cause=self._transport.last_failure,
            )
            failure.interruption_cause = interruption.__cause__
            # Transport retains this same cancellation as its primary. The
            # protected renderer preserves that reference cycle with node IDs.
            raise interruption from failure
        except ExchangeFailed as cause:
            raise ChatFailed(
                "exchange_failed",
                "Cannot complete interpreter request: HTTPS I/O failed; inspect its protected cause and retirement state.",
                cause=cause,
            ) from None
        if response.status != 200:
            raise ChatFailed(
                "http_status",
                f"Cannot complete interpreter request: the model service returned HTTP {response.status}; no tool was accepted.",
                response=response,
                endpoint_error=_endpoint_error(response, messages, self.spec.api_key),
            )
        try:
            document = read_document(response.body, self._response_bytes)
            if type(document) is not dict or type(document.get("choices")) is not list or len(document["choices"]) != 1:
                raise ResponseRejected("Model reply must contain exactly one completion.")
            choice = document["choices"][0]
            if (
                type(choice) is not dict
                or type(choice.get("message")) is not dict
                or choice["message"].get("role") != "assistant"
            ):
                raise ResponseRejected("Model reply must contain one assistant message.")
            if choice.get("finish_reason") == "length":
                raise ChatFailed(
                    "completion_limit",
                    "Cannot interpret the model reply: generation stopped at the service's completion limit; no tool was accepted.",
                    response=response,
                )
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ResponseRejected("Model service did not report a completed assistant reply.")
        except ResponseRejected as cause:
            raise ChatFailed(
                "invalid_response",
                "Cannot interpret the model reply: it is not one complete supported JSON completion; no tool was accepted.",
                cause=cause,
                response=response,
            ) from None
        return choice["message"]
