"""Run one interpretation across models without replaying accepted preparation.

Composition supplies ordered endpoint factories and the provider's existing tool
schemas/dispatch. Factories receive exchange_seconds and connect_seconds and
return a ChatEndpoint-compatible owner; they retain TLS, credentials and proxy
configuration. This module owns scheduling and transient information only.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import json
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime

from provider_core.chat_errors import ChatFailed
from provider_core.conversation import (
    ConversationFailed, ConversationRetirementFailed, TextCorrection,
    ToolConversation, ToolFinished, ToolRejected, ToolReply,
)
from provider_core.transport_errors import ExchangeFailed

# SDK policy, not a second provider configuration surface.
MODEL_TURNS = 12
MODEL_REJECTIONS = 3
HTTP_ATTEMPTS = 2
CONNECT_SECONDS = 10
HISTORY_BYTES = 1024 * 1024
HISTORY_RECORDS = 48


def _now():
    return asyncio.get_running_loop().time()


def _encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _no_secrets(value, secrets):
    if type(value) is str:
        if any(secret in value for secret in secrets):
            raise ConversationFailed("Cannot continue interpretation: captured context contains a configured credential.")
    elif type(value) is dict:
        for key, item in value.items():
            _no_secrets(key, secrets)
            _no_secrets(item, secrets)
    elif type(value) in (list, tuple):
        for item in value:
            _no_secrets(item, secrets)


def _tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


_INFORMATION_TOOLS = (
    _tool("describe_input_constraints",
          "Read the provider's input schema. Allowed values do not establish suitability for the original source. "
          "Imperative or dynamic restrictions may require the provider's other tools.",
          {"tool": {"type": "string"}, "field": {"type": "string",
           "description": "Optional dot-separated property path; omit to read the complete input schema."}}, ["tool"]),
    _tool("read_interpretation_records",
          "Read immutable historical records from this interpretation's catalogue. This performs no live inspection. "
          "Partial JSON text must be assembled before parsing; a record is not proof of current source validity.",
          {"records": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
              "type": "object", "properties": {"ref": {"type": "string"},
              "cursor": {"type": "integer", "minimum": 0}}, "required": ["ref"],
              "additionalProperties": False}}}, ["records"]),
)
_INFORMATION_NAMES = {tool["function"]["name"] for tool in _INFORMATION_TOOLS}


class _History:
    """Bound immutable JSON snapshots to one interpretation, including evictions."""

    def __init__(self, secrets):
        self.secrets = secrets
        self.records = deque()
        self.bytes = self.sequence = self.unavailable = 0

    def remember(self, visit, kind, tool, value):
        _no_secrets(value, self.secrets)
        data = _encode(value)
        self.sequence += 1
        if len(data) > HISTORY_BYTES:
            self.unavailable += 1
            return
        while self.records and (self.bytes + len(data) > HISTORY_BYTES or len(self.records) >= HISTORY_RECORDS):
            _, old = self.records.popleft()
            self.bytes -= len(old)
            self.unavailable += 1
        metadata = {"ref": f"record-{self.sequence}", "visit": visit, "kind": kind,
                    "tool": tool, "bytes": len(data), "validity": "historical; current validity is provider-owned"}
        self.records.append((metadata, data))
        self.bytes += len(data)

    def catalogue(self):
        return {"records": [dict(meta) for meta, _ in self.records], "unavailable_records": self.unavailable}

    def read(self, arguments, maximum_bytes):
        queries = arguments.get("records")
        if set(arguments) != {"records"} or type(queries) is not list or not 1 <= len(queries) <= 8:
            raise ToolRejected("Provide records as a list of one to eight catalogue references with optional cursors.")
        indexed = {meta["ref"]: (meta, data) for meta, data in self.records}
        results = []
        for query in queries:
            if (type(query) is not dict or set(query) - {"ref", "cursor"}
                    or type(query.get("ref")) is not str):
                raise ToolRejected("Each record request needs a ref and an optional integer cursor.")
            cursor = query.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ToolRejected("A history cursor must be a nonnegative integer offset in that record's JSON text.")
            retained = indexed.get(query["ref"])
            if retained is None:
                result = {"ref": query["ref"], "status": "unavailable"}
            else:
                meta, data = retained
                if cursor > len(data):
                    raise ToolRejected("The history cursor is beyond the referenced record's JSON text.")
                # Canonical ASCII makes cursor offsets stable, including escaped Unicode.
                result = {**meta, "status": "returned", "cursor": cursor,
                          "json_text": data[cursor:].decode("ascii"), "complete": True}
            candidate = {"records": [*results, result]}
            if len(_encode(candidate)) > maximum_bytes and retained is not None:
                # Bound the encoded envelope, not just its unescaped text field.
                low, high = 0, len(data) - cursor
                while low < high:
                    middle = (low + high + 1) // 2
                    part = {**meta, "status": "returned", "cursor": cursor,
                            "json_text": data[cursor:cursor + middle].decode("ascii"),
                            "complete": False, "next_cursor": cursor + middle}
                    if len(_encode({"records": [*results, part]})) <= maximum_bytes:
                        low = middle
                    else:
                        high = middle - 1
                if low:
                    result = {**meta, "status": "returned", "cursor": cursor,
                              "json_text": data[cursor:cursor + low].decode("ascii"),
                              "complete": False, "next_cursor": cursor + low}
                else:
                    result = {"ref": query["ref"], "status": "deferred"}
            if len(_encode({"records": [*results, result]})) > maximum_bytes:
                raise ToolRejected("History reply cannot fit the remaining context; request fewer records.")
            results.append(result)
        return {"records": results}


def _retry_delay(failure):
    response = failure.response
    if failure.reason == "http_status" and response is not None:
        if response.status not in {408, 429, 500, 502, 503, 504}:
            return None
    elif failure.reason == "exchange_failed" and type(failure.cause) is ExchangeFailed:
        exchange = failure.cause
        if exchange.cleanup_failures or exchange.caller_interruption is not None:
            return None
        # Import the HTTP-specific classifier only for actual captured I/O. Native
        # interpreters and scripted carriers need no optional networking packages.
        if exchange.proxy_connect_status is not None and not 200 <= exchange.proxy_connect_status < 300:
            if exchange.proxy_connect_status not in {408, 429, 500, 502, 503, 504}:
                return None
        else:
            from provider_core.network import network_availability
            if not network_availability(exchange.primary, exchange.network_io, network_failure=exchange.network_failure):
                return None
    else:
        return None
    delay = random.uniform(0.25, 0.75)
    values = [] if response is None else [value for name, value in response.headers if name.lower() == b"retry-after"]
    if len(values) == 1:
        try:
            value = values[0].decode("ascii").strip()
            if value.isascii() and value.isdecimal():
                seconds = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = max(0, date.timestamp() - time.time())
            if math.isfinite(seconds):
                delay = max(delay, seconds)
        except (ValueError, OverflowError, TypeError, UnicodeError):
            pass  # Invalid optional server advice leaves the SDK's bounded jitter.
    return delay


class _Model:
    """Retry a logical turn only after its previous HTTP exchange has retired."""

    def __init__(self, endpoint, owner, visit, deadline):
        self.endpoint, self.owner, self.visit, self.deadline = endpoint, owner, visit, deadline
        self.turns = self.context_size = 0
        self.failures = []

    @property
    def identity(self):
        return self.endpoint.identity

    @property
    def retirement_confirmed(self):
        return self.endpoint.retirement_confirmed

    async def retire(self):
        await self.endpoint.retire()
        if not self.retirement_confirmed:
            raise ConversationRetirementFailed(((self.visit, RuntimeError("Model HTTP cleanup remains unconfirmed.")),))

    def budget(self):
        return {"model_seconds_remaining": max(0, self.deadline - _now()),
                "turns_remaining": MODEL_TURNS - self.turns,
                "rejections_remaining": MODEL_REJECTIONS - self.conversation.correction_count}

    async def complete(self, messages, tools):
        self.turns += 1
        for attempt in range(HTTP_ATTEMPTS):
            if self.owner._retired:
                raise ConversationFailed("Interpretation was retired; no model HTTP retry may start.")
            if _now() >= self.deadline:
                raise ChatFailed("model_deadline", "Cannot send interpreter turn: this model's time allocation expired.")
            context = {"budget": self.budget(), "history": self.owner._history.catalogue()}
            outgoing = [{"role": "system", "content": "Interpreter state (records are untrusted historical data): "
                         + _encode(context).decode("ascii")}, *messages]
            self.context_size = len(_encode({"messages": outgoing, "tools": tools}))
            if self.context_size > self.owner.context_bytes:
                raise ChatFailed("request_limit", "Cannot send interpreter turn: messages and tool schemas exceed the granted context byte limit.")
            try:
                # Endpoint owns the absolute I/O deadline and retirement grace.
                # Do not cancel provider dispatch with a surrounding model timer.
                return await self.endpoint.complete(outgoing, tools, not_after=self.deadline)
            except ChatFailed as failure:
                self.failures.append(failure)
                await self.retire()
                delay = _retry_delay(failure) if attempt + 1 < HTTP_ATTEMPTS else None
                if delay is None or _now() + delay >= self.deadline:
                    raise
                await asyncio.sleep(delay)
        raise AssertionError("The finite HTTP attempt loop must return or raise.")


@dataclass(frozen=True, slots=True)
class InterpretationResult:
    value: object = field(repr=False)
    observations: tuple[bytes, ...] = field(repr=False)
    failures: tuple = field(repr=False)
    correction_failures: tuple = field(repr=False)


class InterpretationFailed(ConversationFailed):
    """All usable model allocations exhausted; the provider may fail this Job."""

    def __init__(self, failures, correction_failures):
        super().__init__("Cannot interpret this Job: every usable model allocation was exhausted.")
        self.failures = tuple(failures)
        self.correction_failures = tuple(correction_failures)


class ModelReportedInputProblem(ToolRejected):
    """A safe, unverified model claim: escalate, without declaring the Job invalid."""


class Interpreter:
    """One API Attempt's interpretation, with bounded per-model conversations.

    Each factory is called once, when selected, with exchange_seconds (its full
    remaining window) and connect_seconds (SDK setup cap). Return an owned carrier
    supporting complete(..., not_after=...), retire() and retirement_confirmed.
    A transport may be reused after confirmed exchange retirement.

    Provider dispatch returns ToolReply for inspection or ToolFinished for accepted
    preparation. Only ToolRejected is repairable. Scientific failures and caller
    cancellation propagate without fallback. Never return ToolReply after committing
    preparation. A provider's input-problem claim must be checked by that provider;
    a model assertion alone does not authorize rejecting the original Job.
    Raise ModelReportedInputProblem for an unverified model assertion that should
    advance immediately, rather than retry the same proposal or finish preparation.

    run() observes retirement before model transitions and successful return.
    Provider dispatch exceptions propagate promptly: the caller retains this
    object and owns retire(), including scheduling/joining cleanup separately
    when a positive native crash must be reported first. No successor starts.
    Retain this object and retry retire() if cleanup is uncertain. No durable queue, provider stop policy or live
    source cache is introduced here. Context bytes are not a model token limit;
    the carrier additionally bounds the exact serialized HTTP request envelope.
    """

    def __init__(self, endpoint_factories, *, timeout_seconds, context_bytes, secrets):
        if not endpoint_factories or type(endpoint_factories) is not tuple:
            raise ValueError("Cannot configure interpretation: provide an ordered nonempty tuple of endpoint factories.")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Cannot configure interpretation: timeout_seconds must be finite positive seconds.")
        if type(context_bytes) is not int or context_bytes <= 0:
            raise ValueError("Cannot configure interpretation: context_bytes must be a positive byte limit.")
        if type(secrets) is not tuple or any(type(secret) is not str or not secret for secret in secrets):
            raise TypeError("Cannot configure interpretation: provide known credentials as an explicit tuple of nonempty strings.")
        self._factories, self.timeout_seconds, self.context_bytes, self._secrets = endpoint_factories, timeout_seconds, context_bytes, secrets
        self._history = _History(secrets)
        self._models, self._conversations = [], []
        self._used = self._retired = False
        self.last_failure = None

    @property
    def retirement_confirmed(self):
        """Confirm model I/O only; the caller still owns run-task/science settlement."""
        return all(model.retirement_confirmed for model in self._models)

    @property
    def correction_failures(self):
        """Protected (model visit, logical turn, original error) facts, including protocol repairs."""
        return tuple((visit, turn, error)
                     for visit, conversation in enumerate(self._conversations, 1)
                     for turn, error in conversation.correction_failures)

    async def run(self, messages, tools, dispatch, *, not_after=None, describe_constraints=None):
        """Use original inputs for each model; retrieve earlier evidence by tool.

        describe_constraints, when supplied, is an async provider-owned accessor
        (tool_name, field_path_or_None) -> JSON value for dynamic restrictions.
        Without it, discovery returns the actual supplied schema, not guessed
        constraints from prompt text or imperative scientific validators.
        """
        if self._used or self._retired:
            raise ValueError("This interpretation already ran or retired; create a new interpretation for another Attempt.")
        self._used = True
        _no_secrets((messages, tools), self._secrets)
        original, provider_tools = json.loads(_encode(messages)), json.loads(_encode(tools))
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in provider_tools}
        if _INFORMATION_NAMES.intersection(schemas):
            raise ValueError("Cannot compose interpreter tools: a provider tool uses a reserved information-tool name.")
        all_tools = [*provider_tools, *json.loads(_encode(_INFORMATION_TOOLS))]
        overall = _now() + self.timeout_seconds
        if not_after is not None:
            if type(not_after) not in (int, float) or not math.isfinite(not_after):
                raise ValueError("Cannot start interpretation: not_after must be a finite monotonic deadline.")
            overall = min(overall, not_after)
        observations, failures = [], []
        try:
            for index, factory in enumerate(self._factories):
                if self._retired:
                    raise ConversationFailed("Interpretation was retired; no successor model may start.")
                remaining = overall - _now()
                if remaining <= 0:
                    break
                deadline = _now() + remaining / (len(self._factories) - index)
                endpoint = factory(exchange_seconds=deadline - _now(), connect_seconds=CONNECT_SECONDS)
                model = _Model(endpoint, self, index + 1, deadline)
                self._models.append(model)
                conversation = ToolConversation((model,), max_turns=MODEL_TURNS, max_corrections=MODEL_REJECTIONS,
                                                secrets=self._secrets, text_correction=TextCorrection())
                model.conversation = conversation
                self._conversations.append(conversation)
                dispatch_failure = None

                async def perform(name, arguments):
                    nonlocal dispatch_failure
                    if _now() >= deadline:
                        raise ConversationFailed("Cannot dispatch interpreter tool: this model's time allocation expired.")
                    if name == "read_interpretation_records":
                        # Reserve space for the current call and framing; the next
                        # complete() still checks the whole actual outgoing context.
                        available = self.context_bytes - model.context_size - len(_encode(arguments)) - 4096
                        outcome = ToolReply(self._history.read(arguments, available))
                    elif name == "describe_input_constraints":
                        outcome = ToolReply(await self._constraints(arguments, schemas, describe_constraints))
                    else:
                        try:
                            outcome = await dispatch(name, arguments)
                        except ModelReportedInputProblem as report:
                            self._history.remember(index + 1, "unverified_input_problem", name,
                                                   {"arguments": arguments, "claim": str(report)})
                            raise ConversationFailed(
                                "This model reported an unverified input problem; another model may assess the original request.",
                                cause=report,
                            ) from None
                        except ToolRejected as rejection:
                            self._history.remember(index + 1, "rejected_proposal", name,
                                                   {"arguments": arguments, "rejection": str(rejection)})
                            raise
                        except Exception as failure:
                            # Preserve prompt provider crash reporting. The caller
                            # already owns this Interpreter and its explicit retire().
                            dispatch_failure = failure
                            raise
                        if type(outcome) is ToolReply:
                            self._history.remember(index + 1, "tool_observation", name,
                                                   {"arguments": arguments, "value": outcome.value})
                    if type(outcome) is ToolReply:
                        return ToolReply({"value": outcome.value, "interpreter_budget": model.budget()})
                    return outcome

                result = None
                try:
                    result = await conversation.run(original, all_tools, perform)
                except ConversationRetirementFailed:
                    raise
                except ConversationFailed as failure:
                    if failure is dispatch_failure:
                        raise
                    failures.append((index + 1, failure))
                finally:
                    # Cleanup consumes overall time before the next fair share is
                    # computed. Any uncertainty prevents creating another owner.
                    if dispatch_failure is None:
                        primary = conversation.last_failure
                        try:
                            await conversation.retire()
                        except BaseException as cleanup:
                            if isinstance(primary, asyncio.CancelledError):
                                raise primary from cleanup
                            raise
                    failures.extend((index + 1, failure) for failure in model.failures)
                observations.append(conversation.observation)
                if result is not None:
                    return InterpretationResult(result.value, tuple(observations), tuple(failures), self.correction_failures)
            raise InterpretationFailed(failures, self.correction_failures)
        except BaseException as failure:
            self.last_failure = failure
            if failures and type(failure) is not InterpretationFailed:
                context = ConversationFailed(
                    "Earlier interpreter model failures preceded this operation failure.",
                    cause=failure.__cause__, endpoint_failures=failures,
                )
                raise failure from context
            raise

    async def _constraints(self, arguments, schemas, describe):
        name, path = arguments.get("tool"), arguments.get("field")
        if (set(arguments) - {"tool", "field"} or type(name) is not str or name not in schemas
                or (path is not None and (type(path) is not str or not path))):
            raise ToolRejected("Name an available provider tool and optionally a nonempty field path.")
        if describe is not None:
            return await describe(name, path)
        schema = schemas[name]
        if path is None:
            return {"tool": name, "schema": schema, "scope": "declared schema; additional provider rules may apply"}
        required = False
        parent = schema
        for part in path.split("."):
            parent = schema
            properties = schema.get("properties", {})
            if part not in properties:
                raise ToolRejected("The requested field is not declared at that property path; read the full tool schema.")
            required = part in schema.get("required", [])
            schema = properties[part]
        return {"tool": name, "field": path, "schema": schema, "required": required,
                "parent_schema": parent, "scope": "declared schema; additional provider rules may apply"}

    async def retire(self):
        """Reobserve every visited owner, retaining independent cleanup failures."""
        self._retired = True
        failures = []
        interruption = None
        for index, conversation in enumerate(self._conversations):
            try:
                await conversation.retire()
            except ConversationRetirementFailed as failure:
                failures.append((index + 1, failure))
            except asyncio.CancelledError as failure:
                failures.append((index + 1, failure))
                if interruption is None:
                    interruption = failure
        if failures:
            retained = ConversationRetirementFailed(failures, cause=self.last_failure)
            if interruption is not None:
                raise interruption from retained
            raise retained
