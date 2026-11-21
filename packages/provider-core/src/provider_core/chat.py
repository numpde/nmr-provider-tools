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
import reprlib
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


def _json_bytes(json_content):
    return json.dumps(json_content, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


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

        expected_configuration_id_pattern = r"[A-Za-z0-9_.-]{1,128}"

        if not re.fullmatch(expected_configuration_id_pattern, self.configuration_id):
            raise ValueError(f"{reprlib.repr(self.configuration_id)=} should match {expected_configuration_id_pattern=}")

        require_type(self.model, str)

        if not self.model.strip():
            raise ValueError(f"{reprlib.repr(self.model)=} should contain non-whitespace characters")

        if not self.model.isprintable():
            raise ValueError(f"{reprlib.repr(self.model)=} should be printable")

        model_byte_length = len(self.model.encode("utf-8"))
        max_model_bytes = 512

        if not (model_byte_length <= max_model_bytes):
            raise ValueError(f"{model_byte_length=} should be at most {max_model_bytes=}")

        actual_api_key = self.api_key

        require_type(actual_api_key, str)

        if not (self.api_key):
            raise ValueError(f"{len(self.api_key)=} should be positive")

        min_visible_ascii_codepoint, max_visible_ascii_codepoint = 33, 126

        # Capture the first code point outside visible ASCII without printing authorization text
        if not all(
            (
                (min_visible_ascii_codepoint <= (api_key_character_codepoint := ord(character)) <= max_visible_ascii_codepoint)
                for character in self.api_key
            ),
        ):
            raise ValueError(f"{api_key_character_codepoint=} should be between {min_visible_ascii_codepoint=} and {max_visible_ascii_codepoint=}")

        if self.reasoning_effort is not None:
            actual_reasoning_effort = self.reasoning_effort

            require_type(actual_reasoning_effort, str)

            if not (self.reasoning_effort):
                raise ValueError(f"{self.reasoning_effort=} should be nonempty")

            if not self.reasoning_effort.isascii():
                raise ValueError(f"{reprlib.repr(self.reasoning_effort)=} should be ASCII")

            if not self.reasoning_effort.isalnum():
                raise ValueError(f"{reprlib.repr(self.reasoning_effort)=} should be alphanumeric")

        require_type(self.url, str)

        if not all(
            (
                (min_visible_ascii_codepoint <= (url_character_codepoint := ord(character)) <= max_visible_ascii_codepoint)
                for character in self.url
            ),
        ):
            raise ValueError(f"{url_character_codepoint=} should be between {min_visible_ascii_codepoint=} and {max_visible_ascii_codepoint=}")

        try:
            endpoint_url = httpx.URL(self.url)
        except httpx.InvalidURL as cause:
            raise ValueError(f"Cannot parse the endpoint URL for {self.configuration_id=}") from cause

        expected_scheme = "https"

        if not (endpoint_url.scheme == expected_scheme):
            raise ValueError(f"{endpoint_url.scheme=} should match {expected_scheme=}")

        if not (endpoint_url.host):
            raise ValueError(f"{endpoint_url.host=} should be nonempty")

        if not (endpoint_url.userinfo == b""):
            raise ValueError(f"{bool(endpoint_url.userinfo)=} should be False")

        if not ("?" not in self.url):
            raise ValueError(f"{'?' in self.url=} should be False")

        if not ("#" not in self.url):
            raise ValueError(f"{'#' in self.url=} should be False")

        canonical_url = str(endpoint_url)

        if not (canonical_url == self.url):
            raise ValueError(f"{reprlib.repr(self.url)=} should match {reprlib.repr(canonical_url)=}")


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
        raise ValueError(f"{request_bytes=} should be positive")

    require_type(response_bytes, int)

    if not (0 < response_bytes):
        raise ValueError(f"{response_bytes=} should be positive")


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

        request_body_byte_length = len(content)

        # repo-check: allow-literal-guard-error -- reason is the unchanged machine code; message reports the actual request size, bound and no-send state
        if not (request_body_byte_length <= self._request_bytes):
            raise ChatFailed(
                reason="request_limit",
                message=f"{request_body_byte_length=} should be at most {self._request_bytes=}; this turn was not sent",
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
