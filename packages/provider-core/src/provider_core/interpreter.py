"""Run one interpretation across models without replaying accepted preparation.

Interpreter chooses model windows. Each model visit owns its conversation and
HTTP retries. History retains immutable evidence; provider tools remain the
source of scientific rules and current input validity.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import json
import math
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

from provider_core.chat_errors import ChatFailed
from provider_core.conversation import (
    ConversationFailed,
    ConversationHandoff,
    ConversationResult,
    ConversationRetirementFailed,
    TextCorrection,
    ToolConversation,
    ToolFinished,
    ToolHandoff,
    ToolRejected,
    ToolReply,
)
from provider_core.transport_errors import ExchangeFailed

# SDK policy, not a second provider configuration surface.
MODEL_TURNS = 12
MODEL_REJECTIONS = 3
HTTP_ATTEMPTS = 2
CONNECT_SECONDS = 10
HISTORY_BYTES = 1024 * 1024
HISTORY_RECORDS = 48
# Two remaining-time floats may change decimal length before the next request.
_CLOCK_ENCODING_RESERVE_BYTES = 64
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}

# Tool documents are provider-defined JSON; this module owns their envelopes,
# not the scientific fields inside them.
type Document = dict[str, Any]
type ModelFailure = tuple[int, BaseException]
type CorrectionFailure = tuple[int, int, BaseException]
type RecordKind = Literal["rejected_proposal", "tool_observation", "unverified_interpretation_problem"]


class ToolDispatch(Protocol):
    async def __call__(
        self, name: str, arguments: Document, *, not_after: float
    ) -> ToolReply | ToolFinished: ...


class ConstraintReader(Protocol):
    """Return provider metadata, or NotImplemented to use the declared schema."""

    async def __call__(
        self, tool: str, field: str | None, *, not_after: float
    ) -> object: ...


class ModelEndpoint(Protocol):
    @property
    def identity(self) -> bytes: ...

    @property
    def retirement_confirmed(self) -> bool: ...

    async def complete(
        self, messages: list[Document], tools: list[Document], *, not_after: float
    ) -> Document: ...

    async def retire(self) -> None: ...


class EndpointFactory(Protocol):
    """Construct a carrier with provider-owned TLS, credentials and proxy routing."""

    def __call__(self, *, exchange_seconds: float, connect_seconds: float) -> ModelEndpoint: ...


class ProseService:
    """Share one configured prose interpreter's retry delay across Jobs.

    Construct this binding once when the provider captures an endpoint's URL,
    language model, credentials, TLS trust and proxy route. Pass the same
    binding to every Interpreter using those grants. Each visit still gets a
    fresh HTTP owner; this object never owns an Attempt or retries its science.
    Separate bindings share no state, even when their display names match.
    """

    def __init__(self, factory: EndpointFactory):
        self._factory = factory
        self._not_before = 0.0
        self._revision = 0
        self.last_failure: ChatFailed | None = None

    @property
    def context_bytes(self) -> int:
        """Keep the captured carrier's byte allowance available to its caller."""
        return self._factory.context_bytes

    @property
    def retry_seconds(self) -> float:
        """Delay for known dependent Jobs; the HTTP send checks it again."""
        return max(0.0, self._not_before - _now())

    def __call__(self, *, exchange_seconds: float, connect_seconds: float) -> ModelEndpoint:
        return self._factory(exchange_seconds=exchange_seconds, connect_seconds=connect_seconds)

    async def _wait(self, *, deadline: float, stopped: asyncio.Event) -> int:
        while True:
            if stopped.is_set():
                raise ConversationFailed("Prose interpretation was retired; no HTTP request may start.")
            now = _now()
            if now >= deadline:
                raise ChatFailed(
                    "service_wait_deadline",
                    "Cannot contact the prose interpretation service: this Job's time allowance for that service expired while waiting; no request was sent.",
                    cause=self.last_failure,
                )
            if now >= self._not_before:
                return self._revision
            try:
                await asyncio.wait_for(stopped.wait(), timeout=min(self._not_before, deadline) - now)
            except TimeoutError:
                # A concurrent failure may have postponed this same service
                # again. Recheck both that delay and the original deadline.
                continue

    def _postpone(self, failure: ChatFailed, delay: float) -> None:
        self._revision += 1
        self._not_before = max(self._not_before, _now() + delay)
        self.last_failure = failure

    def _recovered(self, revision: int) -> None:
        # A request already in flight when a newer refusal arrived cannot
        # revoke that refusal's Retry-After on behalf of subsequent Jobs.
        if revision == self._revision:
            self._not_before = 0.0
            self.last_failure = None


@dataclass(frozen=True, slots=True)
class InterpretationResult:
    model_visit: int
    value: object = field(repr=False)
    observations: tuple[bytes, ...] = field(repr=False)
    failures: tuple[ModelFailure, ...] = field(repr=False)
    correction_failures: tuple[CorrectionFailure, ...] = field(repr=False)


class InterpretationFailed(ConversationFailed):
    """All usable model allocations exhausted; the provider may fail this Job."""

    def __init__(self, failures, correction_failures):
        super().__init__("Cannot interpret this Job: every usable model allocation was exhausted.")
        self.failures = tuple(failures)
        self.correction_failures = tuple(correction_failures)


class InterpretationInputRejected(ConversationFailed):
    """Initial context cannot be sent; no model or scientific work was started."""


class ModelReportedInterpretationProblem(Exception):
    """Hand off an unverified claim; keep private report details in its cause."""

    def __init__(self, *, model_summary: str):
        super().__init__(model_summary)
        self.model_summary = model_summary


class ModelWindowExpired(Exception):
    """The provider settled expired inspection; another model may be considered.

    Raise only after work admitted under not_after has settled. A timeout alone
    does not grant this signal, and it never cancels or rolls back preparation.
    """


class Interpreter:
    """One API Attempt's interpretation across ordered provider endpoint factories.

    A factory receives the selected model's exchange_seconds and the SDK's short
    connect_seconds. Provider dispatch returns ToolReply for inspection and
    ToolFinished for accepted preparation; ToolRejected requests repair, while
    ModelReportedInterpretationProblem advances an unverified claim to the next model.
    Scientific exceptions propagate without fallback. Preparation is never replayed.

    Providers share a ProseService binding across Jobs to retain endpoint retry
    delays. Plain factories remain suitable for independent one-off callers.

    Normal completion and model transitions observe I/O retirement. On a provider
    dispatch exception, the caller retains this object and owns retire(), allowing
    an established native crash to be reported before cleanup settles. The caller
    always owns run-task joining and scientific-resource settlement. Context byte
    bounds do not prove a model token limit or replace the carrier's HTTP bound.
    """

    def __init__(
        self,
        endpoint_factories: tuple[EndpointFactory, ...],
        *,
        timeout_seconds: float,
        context_bytes: int,
        secrets: tuple[str, ...],
    ):
        if not isinstance(endpoint_factories, tuple) or not endpoint_factories:
            raise ValueError(
                "Cannot configure interpretation: provide an ordered nonempty tuple of endpoint factories."
            )
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Cannot configure interpretation: timeout_seconds must be finite positive seconds.")
        if type(context_bytes) is not int or context_bytes <= 0:
            raise ValueError("Cannot configure interpretation: context_bytes must be a positive byte limit.")
        if not isinstance(secrets, tuple) or any(not isinstance(secret, str) or not secret for secret in secrets):
            raise TypeError(
                "Cannot configure interpretation: provide known credentials as an explicit tuple of nonempty strings."
            )
        self._factories = endpoint_factories
        self.timeout_seconds = timeout_seconds
        self.context_bytes = context_bytes
        self._secrets = secrets
        self._history = _History(secrets)
        self._visits: list[_ModelVisit] = []
        self._used = False
        self._stopped = asyncio.Event()
        self.last_failure: BaseException | None = None

    @property
    def retirement_confirmed(self) -> bool:
        """Confirm model I/O only; the caller still owns run-task/science settlement."""
        return all(visit.retirement_confirmed for visit in self._visits)

    @property
    def failures(self) -> tuple[ModelFailure, ...]:
        """Protected failures observed so far, including before scientific effects."""
        return self._failures()

    @property
    def correction_failures(self) -> tuple[CorrectionFailure, ...]:
        """Protected model-visit, logical-turn and original-error facts, even after retirement."""
        return tuple(
            (visit.number, turn, error)
            for visit in self._visits
            for turn, error in visit.conversation.correction_failures
        )

    @property
    def accepted_result(self) -> InterpretationResult | None:
        """Retain accepted preparation even if subsequent model cleanup fails.

        This is a protected result handoff, not proof of model-I/O retirement or
        permission to execute. The caller still owns resource settlement.
        """
        if not self._visits:
            return None
        outcome = self._visits[-1].outcome
        if type(outcome) is not ConversationResult:
            return None
        return self._result(outcome)

    def _result(self, result: ConversationResult) -> InterpretationResult:
        return InterpretationResult(
            model_visit=self._visits[-1].number,
            value=result.value,
            observations=tuple(visit.conversation.observation for visit in self._visits),
            failures=self._failures(),
            correction_failures=self.correction_failures,
        )

    def _failures(self) -> tuple[ModelFailure, ...]:
        failures = []
        for visit in self._visits:
            if visit.failure is not None:
                failures.append((visit.number, visit.failure))
            if type(visit.outcome) is ConversationHandoff:
                failures.append((visit.number, visit.outcome.reason))
            failures.extend((visit.number, error) for error in visit.http_failures)
        return tuple(failures)

    async def run(
        self,
        messages: list[Document],
        tools: list[Document],
        dispatch: ToolDispatch,
        *,
        not_after: float | None = None,
        describe_constraints: ConstraintReader | None = None,
    ) -> InterpretationResult:
        """Use original inputs per model, with optional provider-owned dynamic constraints."""
        if self._used or self._stopped.is_set():
            raise ValueError("This interpretation already ran or retired; create a new interpretation for another Attempt.")
        self._used = True
        deadline = _now() + self.timeout_seconds
        if not_after is not None:
            if type(not_after) not in (int, float) or not math.isfinite(not_after):
                raise ValueError("Cannot start interpretation: not_after must be a finite monotonic deadline.")
            deadline = min(deadline, not_after)
        try:
            try:
                _no_secrets((messages, tools), self._secrets)
            except ConversationFailed as cause:
                raise InterpretationInputRejected(
                    "Cannot start interpretation: initial context contains a configured credential.",
                    cause=cause,
                ) from cause
            original = json.loads(_encode(messages))
            provider_tools = _ProviderTools(tools, dispatch=dispatch, describe=describe_constraints)
            for index, factory in enumerate(self._factories):
                if self._stopped.is_set():
                    raise ConversationFailed("Interpretation was retired; no successor model may start.")
                now = _now()
                remaining = deadline - now
                if remaining <= 0:
                    break
                window = remaining / (len(self._factories) - index)
                model_deadline = now + window
                visit = _ModelVisit(
                    factory,
                    exchange_seconds=window,
                    number=index + 1,
                    deadline=model_deadline,
                    tools=provider_tools,
                    history=self._history,
                    stopped=self._stopped,
                    context_bytes=self.context_bytes,
                    secrets=self._secrets,
                )
                self._visits.append(visit)
                try:
                    outcome = await visit.run(original)
                except ConversationFailed as failure:
                    if failure is visit.provider_failure:
                        raise
                    continue
                if type(outcome) is ConversationHandoff:
                    continue
                return self._result(outcome)
            raise InterpretationFailed(self._failures(), self.correction_failures)
        except BaseException as failure:
            self.last_failure = failure
            failures = self._failures()
            if failures and not isinstance(failure, InterpretationFailed):
                context = ConversationFailed(
                    "Earlier interpreter model failures preceded this operation failure.",
                    cause=failure.__cause__, endpoint_failures=failures,
                )
                raise failure from context
            raise

    async def retire(self) -> None:
        """Reobserve every visited owner, retaining independent cleanup failures."""
        self._stopped.set()
        failures: list[ModelFailure] = []
        interruption = None
        for visit in self._visits:
            try:
                await visit.conversation.retire()
            except ConversationRetirementFailed as failure:
                failures.append((visit.number, failure))
            except asyncio.CancelledError as failure:
                failures.append((visit.number, failure))
                if interruption is None:
                    interruption = failure
        if failures:
            retained = ConversationRetirementFailed(failures, cause=self.last_failure)
            if interruption is not None:
                raise interruption from retained
            raise retained


def _now() -> float:
    return asyncio.get_running_loop().time()


def _encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _no_secrets(value: object, secrets: tuple[str, ...]) -> None:
    if isinstance(value, str):
        if any(secret in value for secret in secrets):
            raise ConversationFailed(
                "Cannot continue interpretation: captured context contains a configured credential."
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _no_secrets(key, secrets)
            _no_secrets(item, secrets)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _no_secrets(item, secrets)


def _tool(
    name: str, description: str, properties: Document, required: list[str]
) -> Document:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class _HistoryRecord:
    ref: str
    visit: int
    kind: RecordKind
    tool: str
    data: bytes = field(repr=False)

    def metadata(self) -> Document:
        return {
            "ref": self.ref,
            "visit": self.visit,
            "kind": self.kind,
            "tool": self.tool,
            "bytes": len(self.data),
            "validity": "historical; current validity is provider-owned",
        }

    def page(self, start: int, stop: int) -> Document:
        # Canonical ASCII keeps offsets stable even inside escaped Unicode.
        result = {
            **self.metadata(),
            "status": "returned",
            "cursor": start,
            "json_text": self.data[start:stop].decode("ascii"),
            "complete": stop == len(self.data),
        }
        if not result["complete"]:
            result["next_cursor"] = stop
        return result


class _History:
    """Retain one interpretation's snapshots in capture order, within fixed limits."""

    def __init__(self, secrets: tuple[str, ...]):
        self._secrets = secrets
        self._records: OrderedDict[str, _HistoryRecord] = OrderedDict()
        self._bytes = 0
        self._sequence = 0
        self._unavailable = 0

    def remember(self, visit: int, kind: RecordKind, tool: str, value: object) -> None:
        # Screen the whole value before pagination could split a credential.
        _no_secrets(value, self._secrets)
        data = _encode(value)
        self._sequence += 1
        if len(data) > HISTORY_BYTES:
            self._unavailable += 1
            return
        while self._records and (
            self._bytes + len(data) > HISTORY_BYTES or len(self._records) >= HISTORY_RECORDS
        ):
            _, oldest = self._records.popitem(last=False)
            self._bytes -= len(oldest.data)
            self._unavailable += 1
        record = _HistoryRecord(
            ref=f"record-{self._sequence}", visit=visit, kind=kind, tool=tool, data=data
        )
        self._records[record.ref] = record
        self._bytes += len(data)

    def catalogue(self) -> Document:
        return {
            "records": [record.metadata() for record in self._records.values()],
            "unavailable_records": self._unavailable,
        }

    def read(self, arguments: Document, fits: Callable[[Document], bool]) -> Document:
        queries = arguments.get("records")
        if set(arguments) != {"records"} or not isinstance(queries, list) or not 1 <= len(queries) <= 8:
            raise ToolRejected(
                "Provide records as a list of one to eight catalogue references with optional cursors."
            )
        results: list[Document] = []
        for query in queries:
            if (
                not isinstance(query, dict)
                or set(query) - {"ref", "cursor"}
                or not isinstance(query.get("ref"), str)
            ):
                raise ToolRejected("Each record request needs a ref and an optional integer cursor.")
            cursor = query.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ToolRejected(
                    "A history cursor must be a nonnegative integer offset in that record's JSON text."
                )
            record = self._records.get(query["ref"])
            results.append({
                "ref": query["ref"],
                "status": "unavailable" if record is None else "deferred",
            })
        # Every requested reference needs a status even when an earlier page
        # consumes the data allowance. Fill pages around those reserved entries.
        if not fits({"records": results}):
            raise ToolRejected("History reply cannot fit the remaining context; request fewer records.")
        for index, query in enumerate(queries):
            record = self._records.get(query["ref"])
            if record is not None:
                results[index] = self._read_page(
                    record, query.get("cursor", 0), results[:index], results[index + 1:], fits,
                )
        return {"records": results}

    @staticmethod
    def _read_page(
        record: _HistoryRecord, cursor: int,
        previous: list[Document], following: list[Document],
        fits: Callable[[Document], bool],
    ) -> Document:
        if cursor > len(record.data):
            raise ToolRejected("The history cursor is beyond the referenced record's JSON text.")
        complete = record.page(cursor, len(record.data))
        if fits({"records": [*previous, complete, *following]}):
            return complete

        # Find a prefix that fits the encoded envelope, including JSON escaping.
        low, high = cursor, len(record.data)
        while low < high:
            middle = (low + high + 1) // 2
            page = record.page(cursor, middle)
            if fits({"records": [*previous, page, *following]}):
                low = middle
            else:
                high = middle - 1
        if low == cursor:
            return {"ref": record.ref, "status": "deferred"}
        return record.page(cursor, low)


class _InformationTool(Protocol):
    @property
    def definition(self) -> Document: ...

    async def invoke(self, arguments: Document, *, not_after: float) -> ToolReply: ...


class _DescribeInputConstraints:
    """Discover the provider's actual schema or explicitly granted live metadata."""

    def __init__(self, schemas: dict[str, Document], reader: ConstraintReader | None):
        self._schemas = schemas
        self._reader = reader

    @property
    def definition(self) -> Document:
        return _tool(
            "describe_input_constraints",
            "Read the provider's declared schema or supplied dynamic constraints. "
            "Allowed values do not establish suitability for the original source. "
            "Static lookup follows simple property paths, not arbitrary schema references.",
            {
                "tool": {"type": "string"},
                "field": {
                    "type": "string",
                    "description": "Optional dot-separated property path; omit for the complete input schema.",
                },
            },
            ["tool"],
        )

    async def invoke(self, arguments: Document, *, not_after: float) -> ToolReply:
        name, path = arguments.get("tool"), arguments.get("field")
        if (
            set(arguments) - {"tool", "field"}
            or not isinstance(name, str)
            or name not in self._schemas
            or (path is not None and (not isinstance(path, str) or not path))
        ):
            raise ToolRejected("Name an available provider tool and optionally a nonempty field path.")
        if self._reader is not None:
            metadata = await self._reader(name, path, not_after=not_after)
            if metadata is not NotImplemented:
                return ToolReply(metadata)

        schema = self._schemas[name]
        result = {
            "tool": name,
            "scope": "declared schema; additional provider rules may apply",
        }
        if path is None:
            return ToolReply({**result, "schema": schema})
        parent = schema
        required = False
        for part in path.split("."):
            parent = schema
            properties = parent.get("properties", {}) if isinstance(parent, dict) else {}
            if part not in properties:
                raise ToolRejected(
                    "The requested field is not declared at that property path; read the full tool schema."
                )
            schema = properties[part]
            required = part in parent.get("required", [])
        return ToolReply({
            **result,
            "field": path,
            "schema": schema,
            "required": required,
            "parent_schema": parent,
        })


class _ReadInterpretationRecords:
    """Read captured evidence through the visit's complete-context byte budget."""

    def __init__(self, read: Callable[[Document], Document]):
        self._read = read

    @property
    def definition(self) -> Document:
        return _tool(
            "read_interpretation_records",
            "Read immutable historical records from this interpretation's catalogue. "
            "This performs no live inspection. Partial JSON text must be assembled before parsing; "
            "a record is not proof of current source validity.",
            {
                "records": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "cursor": {"type": "integer", "minimum": 0},
                        },
                        "required": ["ref"], "additionalProperties": False,
                    },
                },
            },
            ["records"],
        )

    async def invoke(self, arguments: Document, *, not_after: float) -> ToolReply:
        return ToolReply(self._read(arguments))


class _ProviderTools:
    """Capture the provider's declared capabilities once per interpretation."""

    def __init__(
        self, definitions: list[Document], dispatch: ToolDispatch, describe: ConstraintReader | None
    ):
        self.definitions = json.loads(_encode(definitions))
        self.schemas = {
            tool["function"]["name"]: tool["function"]["parameters"] for tool in self.definitions
        }
        self.dispatch = dispatch
        self.describe = describe


class _Tools:
    """Derive advertised information tools and dispatch from the same instances."""

    def __init__(self, provider: _ProviderTools, information: tuple[_InformationTool, ...]):
        self.provider = provider
        self.information: dict[str, _InformationTool] = {}
        self.definitions = list(provider.definitions)
        for tool in information:
            definition = tool.definition
            name = definition["function"]["name"]
            if name in provider.schemas or name in self.information:
                raise ValueError(
                    "Cannot compose interpreter tools: an information-tool name is already in use."
                )
            self.information[name] = tool
            self.definitions.append(definition)

    async def dispatch(self, name: str, arguments: Document, *, not_after: float) -> ToolReply | ToolFinished:
        if name not in self.provider.schemas:
            raise ToolRejected("Choose a tool from the supplied definitions; this tool was not run.")
        return await self.provider.dispatch(name, arguments, not_after=not_after)


def _retryable(failure: ChatFailed) -> bool:
    if failure.reason == "http_status" and failure.response is not None:
        return failure.response.status in _RETRYABLE_STATUSES
    if failure.reason != "exchange_failed" or type(failure.cause) is not ExchangeFailed:
        return False
    exchange = failure.cause
    if exchange.cleanup_failures or exchange.caller_interruption is not None:
        return False
    status = exchange.proxy_connect_status
    if status is not None and not 200 <= status < 300:
        return status in _RETRYABLE_STATUSES

    # Native-only consumers do not import optional HTTP libraries. Availability
    # classification needs them only when an actual HTTP exchange has failed.
    from provider_core.network import network_availability

    return network_availability(
        exchange.primary, exchange.network_io, network_failure=exchange.network_failure
    )


def _retry_after(failure: ChatFailed) -> float:
    response = failure.response
    values = (
        [] if response is None
        else [value for name, value in response.headers if name.lower() == b"retry-after"]
    )
    if len(values) != 1:
        return 0.0
    try:
        value = values[0].decode("ascii").strip()
        if value.isascii() and value.isdecimal():
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0, date.timestamp() - time.time())
    except (ValueError, OverflowError, TypeError, UnicodeError):
        return 0.0  # Invalid optional advice grants no server-directed delay.
    return seconds if math.isfinite(seconds) else 0.0


class _ModelVisit:
    """Own one fixed model window, its conversation, dispatch and retry cleanup."""

    def __init__(
        self,
        factory: EndpointFactory,
        *,
        exchange_seconds: float,
        number: int,
        deadline: float,
        tools: _ProviderTools,
        history: _History,
        stopped: asyncio.Event,
        context_bytes: int,
        secrets: tuple[str, ...],
    ):
        self.number = number
        self._deadline = deadline
        self._history = history
        self._stopped = stopped
        self._context_bytes = context_bytes
        self.failure: ConversationFailed | None = None
        self.provider_failure: Exception | None = None
        self.outcome: ConversationResult | ConversationHandoff | None = None
        self.http_failures: list[ChatFailed] = []
        reader = tools.describe
        guarded_reader = None
        if reader is not None:
            async def guarded_reader(name: str, field: str | None, *, not_after: float) -> object:
                try:
                    return await reader(name, field, not_after=not_after)
                except (ToolRejected, ModelWindowExpired):
                    raise
                except Exception as failure:
                    self.provider_failure = failure
                    raise

        self._tools = _Tools(tools, (
            _DescribeInputConstraints(tools.schemas, guarded_reader),
            _ReadInterpretationRecords(self._read_history),
        ))
        self._service = factory if isinstance(factory, ProseService) else None
        self._endpoint = factory(exchange_seconds=exchange_seconds, connect_seconds=CONNECT_SECONDS)
        self.conversation = ToolConversation(
            (self,),
            max_turns=MODEL_TURNS,
            max_corrections=MODEL_REJECTIONS,
            secrets=secrets,
            text_correction=TextCorrection(),
        )

    @property
    def identity(self) -> bytes:
        return self._endpoint.identity

    @property
    def retirement_confirmed(self) -> bool:
        return self._endpoint.retirement_confirmed

    def budget(self) -> Document:
        return {
            "model_seconds_remaining": max(0, self._deadline - _now()),
            "turns_remaining": MODEL_TURNS - self.conversation.turns_started,
            "rejections_remaining": MODEL_REJECTIONS - self.conversation.correction_count,
        }

    async def run(self, messages: list[Document]) -> ConversationResult | ConversationHandoff:
        try:
            self.outcome = await self.conversation.run(messages, self._tools.definitions, self._dispatch)
            return self.outcome
        except ConversationFailed as failure:
            if failure is not self.provider_failure:
                self.failure = failure
            raise
        finally:
            # A positive native crash must reach its provider promptly. That
            # caller retains this visit through Interpreter and owns retirement.
            if self.provider_failure is None:
                primary = self.conversation.last_failure
                try:
                    await self.conversation.retire()
                except BaseException as cleanup:
                    if isinstance(primary, asyncio.CancelledError):
                        raise primary from cleanup
                    raise

    async def retire(self) -> None:
        """The endpoint port used by ToolConversation between HTTP attempts."""
        await self._endpoint.retire()
        if not self.retirement_confirmed:
            raise ConversationRetirementFailed(
                ((self.number, RuntimeError("Model HTTP cleanup remains unconfirmed.")),)
            )

    async def complete(self, messages: list[Document], tools: list[Document]) -> Document:
        for attempt in range(HTTP_ATTEMPTS):
            if self._stopped.is_set():
                raise ConversationFailed("Interpretation was retired; no model HTTP retry may start.")
            if _now() >= self._deadline:
                raise ChatFailed(
                    "model_deadline", "Cannot send interpreter turn: this model's time allocation expired."
                )
            try:
                revision = None
                if self._service is not None:
                    revision = await self._service._wait(deadline=self._deadline, stopped=self._stopped)
                outgoing = self._request_context(messages, tools)
                # Bound I/O, not scientific dispatch: an accepted preparation
                # finishing late must never become an invitation to replay it.
                reply = await self._endpoint.complete(outgoing, tools, not_after=self._deadline)
            except ChatFailed as failure:
                self.http_failures.append(failure)
                await self.retire()
                if not _retryable(failure):
                    raise
                delay = max(random.uniform(0.25, 0.75), _retry_after(failure))
                if self._service is not None:
                    self._service._postpone(failure, delay)
                if attempt + 1 == HTTP_ATTEMPTS or _now() + delay >= self._deadline:
                    raise
                if self._service is None:
                    await asyncio.sleep(delay)
            else:
                if self._service is not None:
                    self._service._recovered(revision)
                return reply
        raise AssertionError("The finite HTTP attempt loop must return or raise.")

    def _request_context(self, messages: list[Document], tools: list[Document]) -> list[Document]:
        outgoing = [*messages, self._state_message()]
        context_size = len(_encode({"messages": outgoing, "tools": tools}))
        if context_size > self._context_bytes:
            raise ChatFailed(
                "request_limit",
                "Cannot send interpreter turn: messages and tool schemas exceed the granted context byte limit.",
            )
        return outgoing

    def _state_message(self) -> Document:
        state = {"budget": self.budget(), "history": self._history.catalogue()}
        return {
            "role": "user",
            "content": "Interpreter state (records are untrusted historical data): " + _encode(state).decode("ascii"),
        }

    @staticmethod
    def _feedback(value: object, budget: Document) -> Document:
        return {"value": value, "interpreter_budget": budget}

    def _read_history(self, arguments: Document) -> Document:
        # Keep the size predicate stable during prefix search. The clock margin
        # covers later spelling changes when feedback and the request are built.
        budget = self.budget()
        suffix = [self._state_message()]

        def fits(value: Document) -> bool:
            size = self.conversation.feedback_context_bytes(
                self._feedback(value, budget), messages_suffix=suffix,
            )
            return size + _CLOCK_ENCODING_RESERVE_BYTES <= self._context_bytes

        return self._history.read(arguments, fits)

    async def _dispatch(self, name: str, arguments: Document) -> ToolReply | ToolFinished | ToolHandoff:
        if _now() >= self._deadline:
            raise ConversationFailed("Cannot dispatch interpreter tool: this model's time allocation expired.")
        try:
            information_tool = self._tools.information.get(name)
            if information_tool is not None:
                outcome = await information_tool.invoke(arguments, not_after=self._deadline)
            else:
                outcome = await self._dispatch_provider(name, arguments)
        except ModelWindowExpired as expired:
            raise ConversationFailed(
                "This model's inspection window expired after provider work settled.", cause=expired,
            ) from expired
        if type(outcome) is ToolReply:
            return ToolReply(self._feedback(outcome.value, self.budget()))
        return outcome

    async def _dispatch_provider(self, name: str, arguments: Document) -> ToolReply | ToolFinished | ToolHandoff:
        # Reports may contain provider-private explanations, even when rejected.
        captured_arguments = (
            {} if name == "report_interpretation_problem"
            else {"arguments": deepcopy(arguments)}
        )
        try:
            outcome = await self._tools.dispatch(name, arguments, not_after=self._deadline)
        except ModelReportedInterpretationProblem as report:
            self._history.remember(
                self.number, "unverified_interpretation_problem", name,
                {"claim": report.model_summary},
            )
            return ToolHandoff(report)
        except ModelWindowExpired:
            raise
        except ToolRejected as rejection:
            self._history.remember(
                self.number, "rejected_proposal", name,
                {**captured_arguments, "rejection": str(rejection)},
            )
            raise
        except Exception as failure:
            self.provider_failure = failure
            raise
        if type(outcome) is ToolReply:
            self._history.remember(
                self.number, "tool_observation", name,
                {**captured_arguments, "value": outcome.value},
            )
        return outcome
