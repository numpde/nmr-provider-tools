"""Keep tool turns, corrections and endpoint fallback in one bounded transcript.

The transcript/correction boundary follows SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/interpreter.py:40-151. This implementation owns no
scientific tools or report policy. The caller owns its aggregate deadline and
run-task lifetime; each endpoint owns bounded I/O and confirmed retirement.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from provider_core.chat import ChatFailed
from provider_core.response_json import ResponseRejected, read_document
from provider_core.transport import ExchangeFailed


def _encode(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True)
class ToolReply:
    value: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class ToolFinished:
    value: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class TextCorrection:
    """Continue rejected turns with plain diagnostic text and this user instruction."""

    user_content: str = field(repr=False)

    def __post_init__(self):
        if type(self.user_content) is not str:
            raise TypeError("Text correction requires a user instruction supplied as text.")
        if not self.user_content:
            raise ValueError("Text correction requires a nonempty user instruction.")


@dataclass(frozen=True, slots=True)
class ConversationResult:
    value: object = field(repr=False)
    observation: bytes = field(repr=False)
    endpoint_failures: tuple = field(default=(), repr=False)
    correction_failures: tuple = field(default=(), repr=False)


class ToolRejected(ValueError):
    """An owned, public correction reason; never raw model or worker diagnostics."""


class ConversationFailed(RuntimeError):
    """Safe context with original private failures, separate from public routes."""

    # silent-defaults: allow-contract -- Local conversation refusals can precede any endpoint failure; an empty history records that fact without inventing an endpoint.
    def __init__(self, message, *, cause=None, endpoint_failures=(), correction_failures=()):
        super().__init__(message)
        self.cause = cause
        self.endpoint_failures = tuple(endpoint_failures)
        self.correction_failures = tuple(correction_failures)


class ConversationRetirementFailed(RuntimeError):
    """Retain the operation and every independently observed endpoint failure."""

    # silent-defaults: allow-contract -- Retirement can fail before any model operation fails; cleanup causes remain separate from the possibly empty operation history.
    def __init__(self, failures, *, cause=None, endpoint_failures=(), correction_failures=()):
        super().__init__("Cannot confirm interpreter connection cleanup; retain its endpoints and private failure evidence.")
        self.failures = tuple(failures)
        self.cause = cause
        self.interruption_cause = None
        self.endpoint_failures = tuple(endpoint_failures)
        self.correction_failures = tuple(correction_failures)


def independent_interruption_failure(failure):
    """Identify retained faults beyond the original model-request cancellation.

    Inspect after retirement: the exchange can acquire a late I/O or cleanup
    failure while its original operation settles. Returning None makes no claim
    about scientific effects or their outcome; this observation has no independent
    fault.
    """
    if not isinstance(failure, asyncio.CancelledError):
        return failure
    retained = failure.__cause__
    # This owner can add earlier endpoint/correction history around the original
    # cause. Unwrap that one known carrier, never arbitrary exception graphs.
    cause = retained.cause if type(retained) is ConversationFailed else retained
    if cause is None:
        return None
    if type(cause) is ChatFailed and cause.reason == "request_cancelled" and cause.interruption_cause is None:
        exchange = cause.cause
        if (
            type(exchange) is ExchangeFailed
            and exchange.primary is failure
            and not exchange.cleanup_failures
            and (exchange.response_head is None or exchange.response_head.status == 200)
        ):
            return None
    return retained


def require_turn_limit(max_turns):
    """Admit the conversation budget before startup acquires model carriers."""
    if not (type(max_turns) is int and 0 < max_turns):
        raise ValueError("Cannot configure interpretation: the total model-turn limit must be a positive integer.")


class ToolConversation:
    """One serial conversation; only a carrier failure permits endpoint fallback.

    dispatch is awaited exactly once for an admitted call, returning ToolReply or
    ToolFinished. Only ToolRejected requests correction. The caller supplies all
    known endpoint secrets; transcripts are transient and never observations.
    retire() must also be observed after failure or cancellation. It may recheck
    cleanup, but neither run nor scientific effects can restart on this object.
    TextCorrection additionally permits plain assistant turns to request repair
    and retains canonical admitted arguments after caller rejection. Invalid or
    unparsed argument text is still erased before continuation. Omitting the
    grant keeps JSON rejection feedback and strict tool-call admission.
    """

    def __init__(self, endpoints, *, max_turns, secrets, text_correction=None):
        if type(endpoints) is not tuple or not endpoints:
            raise ValueError("Cannot configure interpretation: provide a nonempty ordered tuple of endpoints.")
        require_turn_limit(max_turns)
        if type(secrets) is not tuple or any(type(secret) is not str or not secret for secret in secrets):
            raise TypeError("Cannot configure interpretation: known secrets must be an explicit tuple of nonempty strings.")
        if text_correction is not None and type(text_correction) is not TextCorrection:
            raise TypeError("Cannot configure interpretation: text_correction must be TextCorrection or None.")
        self._endpoints, self._max_turns = endpoints, max_turns
        self._text_correction = text_correction
        self._secrets = tuple(sorted(set(secrets), key=len, reverse=True))
        self._used = self._retired = False
        self._turns = self._corrections = 0
        self._seen_ids = set()
        self._routes = []
        self._messages = []
        self._tools = []
        self._tools_sha256 = None
        self.last_rejection = None
        self.last_failure = None
        self.endpoint_failures = []
        # One rejection per started turn bounds this private, one-based history.
        self.correction_failures = []

    def redact(self, text):
        """Remove configured keys; this does not classify arbitrary private data."""
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def _require_no_secrets(self, value):
        if type(value) is str:
            if any(secret in value for secret in self._secrets):
                raise ConversationFailed("Cannot continue interpretation: its outgoing context contains a configured credential.")
        elif type(value) is dict:
            for key, item in value.items():
                self._require_no_secrets(key)
                self._require_no_secrets(item)
        elif type(value) in (tuple, list):
            for item in value:
                self._require_no_secrets(item)

    async def run(self, messages, tools, dispatch):
        if self._used or self._retired:
            raise ValueError("This interpreter conversation already ran or retired; create a new owned conversation.")
        self._used = True
        try:
            return await self._run(messages, tools, dispatch)
        except BaseException as failure:
            # Retirement must retain the failed operation even when a later,
            # independent endpoint cleanup also fails.
            self.last_failure = failure
            history = tuple(self.endpoint_failures)
            corrections = tuple(self.correction_failures)
            if type(failure) in (ConversationFailed, ConversationRetirementFailed):
                failure.endpoint_failures = history
                failure.correction_failures = corrections
            elif history or corrections:
                # Keep foreign/scientific exception identity and classification.
                # This owned context retains earlier interpreter failures and the
                # previous explicit cause without pointing back to the primary.
                context = ConversationFailed(
                    "Earlier interpreter failures preceded this operation failure.",
                    cause=failure.__cause__,
                    endpoint_failures=history,
                    correction_failures=corrections,
                )
                raise failure from context
            raise

    async def _run(self, messages, tools, dispatch):
        self._require_no_secrets(messages)
        self._require_no_secrets(tools)
        if self._text_correction is not None:
            self._require_no_secrets(self._text_correction.user_content)
        self._messages, self._tools = (
            json.loads(_encode(messages)),
            json.loads(_encode(tools)),
        )
        self._tools_sha256 = hashlib.sha256(_encode(tools)).hexdigest()
        index = 0
        self._route(self._endpoints[index])
        while self._turns < self._max_turns:
            if self._retired:
                raise ConversationFailed("Interpretation retired before a decision could be accepted.")
            endpoint = self._endpoints[index]
            self._turns += 1
            self._routes[-1]["turns_started"] += 1
            try:
                message = await endpoint.complete(self._messages, self._tools)
                message, calls = self._admit_carrier(message)
            except (ChatFailed, ConversationFailed) as failure:
                self.last_failure = failure
                # Keep each bounded private observation, including failures
                # before a successful fallback. Public routes contain no body.
                self.endpoint_failures.append((index, failure))
                self._routes[-1]["failure"] = "invalid_tool_carrier"
                if isinstance(failure, ChatFailed):
                    self._routes[-1]["failure"] = failure.reason
                    if failure.response is not None:
                        self._routes[-1]["http_status"] = failure.response.status
                await self._retire_endpoint(index, cause=failure)
                if index + 1 == len(self._endpoints) or self._turns == self._max_turns:
                    raise ConversationFailed(
                        "Cannot interpret the request: no usable model reply was obtained within the configured endpoints and turn budget.",
                        cause=failure,
                    ) from None
                index += 1
                self._route(self._endpoints[index])
                continue
            self._require_active()
            self._messages.append(message)
            if len(calls) != 1:
                for call in calls:
                    call["function"]["arguments"] = "{}"
                self._correct_turn(calls, ToolRejected("Choose exactly one tool per turn; no operations were performed."))
                continue
            call = calls[0]
            try:
                arguments = self._arguments(call["function"]["arguments"])
            except ToolRejected as rejection:
                # Invalid argument text may contain encoded credentials. Keep
                # its call identity and safe correction, not that raw text.
                call["function"]["arguments"] = "{}"
                self._correct_turn(calls, rejection)
                continue
            call["function"]["arguments"] = _encode(arguments).decode("ascii")
            try:
                outcome = await dispatch(call["function"]["name"], arguments)
            except ToolRejected as rejection:
                if self._text_correction is None:
                    call["function"]["arguments"] = "{}"
                self._correct_turn(calls, rejection)
                continue
            self._require_active()
            if type(outcome) is ToolFinished:
                return ConversationResult(
                    outcome.value, self._observation(), tuple(self.endpoint_failures), tuple(self.correction_failures)
                )
            if type(outcome) is not ToolReply:
                raise TypeError("Interpreter tool dispatch must return ToolReply or ToolFinished.")
            self._feedback(call["id"], outcome.value)
        # silent-defaults: allow-contract -- Absent usable correction text reports exhaustion without choosing another endpoint or input.
        reason = self.last_rejection or "No tool submitted a supported input."
        raise ConversationFailed(
            f"Cannot interpret the request: the {self._max_turns}-turn budget was exhausted. Last correction: {reason}"
        )

    def _route(self, endpoint):
        # Identity comes from the trusted adapter, not model response fields.
        identity = json.loads(endpoint.identity)
        identity = {key: self.redact(value) if type(value) is str else value for key, value in identity.items()}
        self._routes.append({"endpoint": identity, "turns_started": 0})

    def _require_active(self):
        if self._retired:
            raise ConversationFailed("Interpretation retired before a decision could be accepted.")

    def _admit_carrier(self, message):
        if type(message) is not dict or message.get("role") != "assistant":
            raise ConversationFailed("Cannot interpret model action: the reply is not an assistant message.")
        canonical = {"role": "assistant"}
        for text_field in ("content", "reasoning_content"):
            if text_field in message:
                if message[text_field] is not None and type(message[text_field]) is not str:
                    raise ConversationFailed("Cannot interpret model action: assistant text has a malformed carrier.")
                self._require_no_secrets(message[text_field])
                canonical[text_field] = message[text_field]
        calls = message.get("tool_calls")
        if calls is None and self._text_correction is not None:
            return canonical, []
        if type(calls) is not list or not calls:
            raise ConversationFailed("Cannot interpret model action: the reply supplies no identifiable tool call.")
        identifiers = set()
        admitted = []
        for call in calls:
            if (
                type(call) is not dict
                or type(call.get("id")) is not str
                or re.fullmatch(r"[!-~]{1,128}", call["id"]) is None
                or call["id"] in self._seen_ids
                or call["id"] in identifiers
                or call.get("type") != "function"
                or type(call.get("function")) is not dict
                or type(call["function"].get("name")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", call["function"]["name"]) is None
                or type(call["function"].get("arguments")) is not str
            ):
                raise ConversationFailed(
                    "Cannot interpret model action: tool identity, function or argument carrier is malformed or repeated."
                )
            identifiers.add(call["id"])
            self._require_no_secrets((call["id"], call["function"]["name"]))
            admitted.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    },
                }
            )
        canonical["tool_calls"] = admitted
        self._seen_ids.update(identifiers)
        return canonical, admitted

    def _arguments(self, text):
        try:
            # The enclosing consumed response already bounds this string.
            raw = text.encode("utf-8")
            arguments = read_document(raw, len(raw))
            _encode(arguments)
        except (
            ResponseRejected,
            UnicodeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as cause:
            raise ToolRejected("Tool arguments must be one JSON object with unique fields and finite values.") from cause
        if type(arguments) is not dict:
            raise ToolRejected("Tool arguments must be one JSON object.")
        self._require_no_secrets(arguments)
        return arguments

    def _feedback(self, identifier, value):
        self._require_no_secrets(value)
        self._tool_message(identifier, _encode(value).decode("ascii"))

    def _tool_message(self, identifier, content):
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": identifier,
                "content": content,
            }
        )

    def _correct_turn(self, calls, rejection):
        reason = str(rejection)
        self._require_no_secrets(reason)
        self.last_rejection = reason
        self._corrections += 1
        self.correction_failures.append((self._turns, rejection))
        content = reason
        if self._text_correction is None:
            content = _encode({"rejected": reason}).decode("ascii")
        for call in calls:
            self._tool_message(call["id"], content)
        if self._text_correction is not None:
            self._messages.append({"role": "user", "content": self._text_correction.user_content})

    def _observation(self):
        return _encode(
            {
                "tools_sha256": self._tools_sha256,
                "turn_limit": self._max_turns,
                "turns_started": self._turns,
                "corrections": self._corrections,
                "routes": self._routes,
            }
        )

    async def _retire_endpoint(self, index, *, cause=None):
        endpoint = self._endpoints[index]
        try:
            await endpoint.retire()
            if not endpoint.retirement_confirmed:
                raise RuntimeError("Interpreter endpoint has not confirmed resource retirement.")
        except (Exception, asyncio.CancelledError) as cleanup:
            failure = ConversationRetirementFailed(
                ((index, cleanup),),
                cause=cause,
                endpoint_failures=self.endpoint_failures,
                correction_failures=self.correction_failures,
            )
            if isinstance(cleanup, asyncio.CancelledError):
                failure.interruption_cause = cleanup.__cause__
                raise cleanup from failure
            raise failure from None

    async def retire(self):
        self._retired = True
        primary = self.last_failure
        failures = []
        interruption = None
        for index in range(len(self._endpoints)):
            try:
                await self._retire_endpoint(index)
            except ConversationRetirementFailed as failure:
                failures.extend(failure.failures)
            except asyncio.CancelledError as cause:
                failures.append((index, cause))
                if interruption is None:
                    interruption = cause
        if failures:
            failure = ConversationRetirementFailed(
                failures,
                cause=primary,
                endpoint_failures=self.endpoint_failures,
                correction_failures=self.correction_failures,
            )
            self.last_failure = failure
            if interruption is not None:
                failure.interruption_cause = interruption.__cause__
                raise interruption from failure
            raise failure from None
