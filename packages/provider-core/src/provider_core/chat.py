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
import ssl
from dataclasses import dataclass, field

import httpx

from provider_core.chat_errors import ChatFailed
from provider_core.adapters.chat_completions.encoding import context_byte_count, request_body
from provider_core.adapters.chat_completions.request import completion_request
from provider_core.adapters.chat_completions.response import read_completion
from provider_core.transport import ExchangeFailed
from provider_core.chat_process import SupervisedChatTransport
from provider_core.type_requirements import require_type


# Retain the existing local ceiling for optional service diagnostic JSON
# repo-check: allow-module-constant
_ERROR_DETAIL_BYTES = 8192


def _json_bytes(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatEndpointSpec:
    """Grant one complete endpoint URL without ambient configuration lookup.

    Identifier/model bounds, header-safe keys and the reasoning word grammar
    restrict this SDK's configuration; they are not universal service limits.
    Composition supplies the complete canonical HTTPS URL. The wire adapter
    neither appends a route nor chooses an endpoint or reasoning vocabulary.
    """

    configuration_id: str
    model: str
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    reasoning_effort: str | None

    def __post_init__(self):
        require_type(self.configuration_id, str)

        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.configuration_id) is None:
            raise ValueError("Cannot configure interpreter endpoint: configuration_id must be a bounded ASCII identifier.")

        require_type(self.model, str)

        if not (self.model.strip() and self.model.isprintable() and (len(self.model.encode('utf-8')) <= 512)):
            raise ValueError("Cannot configure interpreter endpoint: model must be nonblank printable UTF-8 text within 512 bytes.")

        actual_api_key = self.api_key

        require_type(actual_api_key, str)

        if (not (self.api_key)) or (any((not 32 < ord(char) < 127) for char in self.api_key)):
            raise ValueError("Cannot configure interpreter endpoint: an explicit header-safe API key is required.")

        if self.reasoning_effort is not None:
            actual_reasoning_effort = self.reasoning_effort

            require_type(actual_reasoning_effort, str)

            if not (self.reasoning_effort):
                raise ValueError("Cannot configure interpreter endpoint: reasoning_effort must be an explicit ASCII word or None.")

            if not self.reasoning_effort.isascii():
                raise ValueError("Cannot configure interpreter endpoint: reasoning_effort must be an explicit ASCII word or None.")

            if not self.reasoning_effort.isalnum():
                raise ValueError("Cannot configure interpreter endpoint: reasoning_effort must be an explicit ASCII word or None.")

        require_type(self.url, str)

        if any((not 32 < ord(char) < 127) for char in self.url):
            raise ValueError("Cannot configure interpreter endpoint: URL must be canonical ASCII HTTPS text.")

        try:
            url = httpx.URL(self.url)
        except httpx.InvalidURL:
            raise ValueError("Cannot configure interpreter endpoint: URL is not valid HTTPS.") from None

        if (url.scheme != "https") or (not url.host) or (url.userinfo) or ("?" in self.url) or ("#" in self.url) or (str(url) != self.url):
            raise ValueError("Cannot configure interpreter endpoint: use a canonical HTTPS URL without credentials, query or fragment.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatEndpointFactory:
    """Bind captured endpoint grants; each model visit receives fresh HTTP ownership."""

    spec: ChatEndpointSpec = field(repr=False)
    trust: ssl.SSLContext = field(repr=False)
    retirement_seconds: float
    request_bytes: int
    response_bytes: int
    proxy_socket: str | None = field(default=None, repr=False)
    error_response_bytes: int | None = None

    @property
    def context_bytes(self):
        """Exact messages/tools capacity after reserving this carrier's envelope."""
        empty_context_byte_count = context_byte_count(messages=[], tools=[])

        empty_request = request_body(
            model=self.spec.model,
            messages=[],
            tools=[],
            reasoning_effort=self.spec.reasoning_effort,
        )

        overhead = len(empty_request) - empty_context_byte_count

        return self.request_bytes - overhead

    def __call__(self, *, exchange_seconds, connect_seconds):
        return ChatEndpoint(
            self.spec,
            transport=SupervisedChatTransport(
                trust=self.trust,
                exchange_seconds=exchange_seconds,
                connect_seconds=connect_seconds,
                retirement_seconds=self.retirement_seconds,
                proxy_socket=self.proxy_socket,
            ),
            request_bytes=self.request_bytes,
            response_bytes=self.response_bytes,
            error_response_bytes=self.error_response_bytes,
        )


def require_chat_limits(*, request_bytes, response_bytes):
    """Admit carrier limits before startup gives it a transport."""

    require_type(request_bytes, int)

    if not (0 < request_bytes):
        raise ValueError("Cannot configure interpreter carrier: request and response byte limits must be positive integers.")

    require_type(response_bytes, int)

    if not (0 < response_bytes):
        raise ValueError("Cannot configure interpreter carrier: request and response byte limits must be positive integers.")


class ChatEndpoint:
    """One model carrier with caller-granted transport and explicit byte limits.

    The owner must call retire(), including after cancellation or failed exchange.
    retirement_confirmed must be true before the chooser can switch endpoints.
    No API/Upload credentials, scientific arrays or filesystem access are granted.
    """

    def __init__(self, spec, *, transport, request_bytes, response_bytes, error_response_bytes=None):
        require_type(spec, ChatEndpointSpec)

        require_chat_limits(request_bytes=request_bytes, response_bytes=response_bytes)

        if error_response_bytes is None:
            error_response_bytes = response_bytes

        require_chat_limits(request_bytes=request_bytes, response_bytes=error_response_bytes)
        (self.spec, self._transport) = (spec, transport)
        (self._request_bytes, self._response_bytes) = (request_bytes, response_bytes)
        self._error_response_bytes = error_response_bytes

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
            "error_response_bytes": self._error_response_bytes,
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

    async def complete(self, messages, tools, *, not_after=None):
        """Send one bounded turn under an optional absolute request deadline.

        Response bodies are decoded; retained headers still describe the wire
        entity. not_after bounds request work; the transport separately owns one
        configured cleanup allowance for the original exchange.
        """

        content = request_body(
            model=self.spec.model,
            messages=messages,
            tools=tools,
            reasoning_effort=self.spec.reasoning_effort,
        )

        if len(content) > self._request_bytes:
            content_bytes = len(content)
            content_byte_limit = self._request_bytes
            raise ChatFailed(
                "request_limit",
                f"Cannot send interpreter request: {content_bytes = } exceeds {content_byte_limit = }; this turn was not sent.",
            )

        wire = completion_request(url=self.spec.url, api_key=self.spec.api_key, content=content)

        try:
            response = await self._transport.exchange_decoded(
                wire,
                maximum_bytes=self._response_bytes,
                error_maximum_bytes=self._error_response_bytes,
                owner=self,
                not_after=not_after,
            )
        except asyncio.CancelledError as interruption:
            failure = ChatFailed(
                "request_cancelled",
                "Interpretation was interrupted; the model request may have been processed, but no tool was accepted. No resend was attempted.",
                cause=self._transport.last_failure,
            )

            failure.interruption_cause = interruption.__cause__

            # Transport retains this same cancellation as its primary.
            # The protected renderer preserves that reference cycle with node IDs.
            raise interruption from failure
        except ExchangeFailed as cause:
            raise ChatFailed(
                "exchange_failed",
                "Cannot complete interpreter request: HTTPS I/O failed; inspect its protected cause and retirement state.",
                cause=cause,
            ) from None

        return read_completion(
            response,
            maximum_bytes=self._response_bytes,
            error_detail_maximum_bytes=_ERROR_DETAIL_BYTES,
            messages=messages,
            api_key=self.spec.api_key,
        )
