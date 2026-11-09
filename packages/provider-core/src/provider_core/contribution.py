"""Durable, typed boundary implemented by scientific contributions.

Descriptions are immutable data that orchestration can inspect without importing
scientific dependencies. Bindings stay inside the scientific host and preserve
provider-owned Python types across decoding, state binding, science and encoding.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, get_args, get_origin, get_type_hints

from provider_core.payload import (
    CallRejected,
    DataclassStateBinding,
    JsonDataclassCodec,
    PayloadCodec,
    StateBinding,
)


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identity(value: object, label: str) -> None:
    if not (
        type(value) is str
        and value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError(
            f"{label} must be a nonempty identity without whitespace "
            "or control characters"
        )


def _digest(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase sha256:<64 hex characters> digest")


def _records(values: object, record_type: type[object], label: str) -> None:
    if not (
        type(values) is tuple
        and all(type(value) is record_type for value in values)
    ):
        raise TypeError(f"{label} must be an immutable tuple of {record_type.__name__} records")


def _unique(values: tuple[object, ...], key: Callable[[object], object], label: str) -> None:
    identities = tuple(key(value) for value in values)
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} cannot contain duplicates")


def _callback(value: object, label: str) -> None:
    if not callable(value):
        raise TypeError(f"{label} must be callable")


def _require_scientific_signature(call, input_type, state_type, output_type) -> None:
    signature = inspect.signature(call)
    parameters = tuple(signature.parameters.values())
    if len(parameters) != 5 or any(
        parameter.kind
        not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for parameter in parameters
    ):
        raise TypeError(
            "A scientific call must accept instance, analysis, input, state and control"
        )
    hints = get_type_hints(call, include_extras=True)
    for parameter, expected, label in (
        (parameters[2], input_type, "input"),
        (parameters[3], state_type, "state"),
    ):
        if hints.get(parameter.name) is not expected:
            raise TypeError(
                f"Scientific call {label} annotation must be exactly "
                f"{expected.__qualname__}"
            )
    returned = hints.get("return")
    if (
        get_origin(returned) is not ScientificReturn
        or get_args(returned) != (output_type,)
    ):
        raise TypeError(
            "Scientific call return annotation must be "
            f"ScientificReturn[{output_type.__qualname__}]"
        )


class StateUse(StrEnum):
    BORROW = "borrow"
    CONSUME = "consume"


class IsolationRequirement(StrEnum):
    """Minimum process boundary around one initialized implementation."""

    SHARED_PROCESS = "shared_process"
    DEDICATED_PROCESS = "dedicated_process"


class ConcurrencyRequirement(StrEnum):
    """Where scientific calls must be serialized."""

    INSTANCE_SERIAL = "instance_serial"
    PROCESS_SERIAL = "process_serial"
    PARALLEL = "parallel"


class InterruptionSupport(StrEnum):
    """Whether a call cooperates with the narrow stop signal."""

    NONE = "none"
    COOPERATIVE = "cooperative"


@dataclass(frozen=True, slots=True)
class ConfigurationDescription:
    format: str
    digest: str

    def __post_init__(self) -> None:
        _identity(self.format, "Configuration format")
        _digest(self.digest, "Configuration digest")


@dataclass(frozen=True, slots=True)
class AssetDescription:
    name: str
    media_type: str
    digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        _identity(self.name, "Asset name")
        _identity(self.media_type, "Asset media type")
        _digest(self.digest, "Asset digest")
        if not (type(self.size_bytes) is int and 0 < self.size_bytes):
            raise ValueError("Asset byte count must be a positive integer")


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    """Neutral quantity that deployment must grant to the scientific host."""

    kind: str
    amount: int
    unit: str
    selector: str | None = None
    exclusive: bool = False

    def __post_init__(self) -> None:
        _identity(self.kind, "Resource kind")
        _identity(self.unit, "Resource unit")
        if not (type(self.amount) is int and 0 < self.amount):
            raise ValueError("Resource amount must be a positive integer")
        if self.selector is not None:
            _identity(self.selector, "Resource selector")
        if type(self.exclusive) is not bool:
            raise TypeError("Resource exclusivity must be boolean")

    @property
    def identity(self) -> tuple[str, str, str | None]:
        return self.kind, self.unit, self.selector


@dataclass(frozen=True, slots=True)
class StateParameter:
    name: str
    kind: str
    use: StateUse

    def __post_init__(self) -> None:
        _identity(self.name, "State parameter name")
        _identity(self.kind, "Scientific state kind")
        if type(self.use) is not StateUse:
            raise TypeError("State use must be a declared StateUse")


@dataclass(frozen=True, slots=True)
class StateOutput:
    name: str
    kind: str

    def __post_init__(self) -> None:
        _identity(self.name, "Output state name")
        _identity(self.kind, "Output scientific state kind")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Bounded, provider-reviewed progress fact with no control authority."""

    stage: str
    completed: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        _identity(self.stage, "Progress stage")
        if (self.completed is None) != (self.total is None):
            raise ValueError("Progress completed and total must be supplied together")
        if self.completed is not None and not (
            type(self.completed) is int
            and type(self.total) is int
            and 0 <= self.completed <= self.total
            and 0 < self.total
        ):
            raise ValueError("Progress counts must satisfy 0 <= completed <= total and total > 0")


class ScientificControl(Protocol):
    """Only optional operational capability visible to a scientific adapter."""

    @property
    def stop_requested(self) -> bool: ...

    def report_progress(self, event: ProgressEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class CallDescription:
    name: str
    input_format: str
    result_format: str
    state_inputs: tuple[StateParameter, ...]
    state_outputs: tuple[StateOutput, ...]
    resources: tuple[ResourceRequirement, ...]
    concurrency: ConcurrencyRequirement
    interruption: InterruptionSupport
    progress: bool

    def __post_init__(self) -> None:
        _identity(self.name, "Call name")
        _identity(self.input_format, "Input format")
        _identity(self.result_format, "Result format")
        _records(self.state_inputs, StateParameter, "State parameters")
        _unique(self.state_inputs, lambda value: value.name, "State parameters")
        _records(self.state_outputs, StateOutput, "State outputs")
        _unique(self.state_outputs, lambda value: value.name, "State outputs")
        _records(self.resources, ResourceRequirement, "Call resource requirements")
        _unique(self.resources, lambda value: value.identity, "Call resource requirements")
        if type(self.concurrency) is not ConcurrencyRequirement:
            raise TypeError("Call concurrency must be a declared ConcurrencyRequirement")
        if type(self.interruption) is not InterruptionSupport:
            raise TypeError("Call interruption must be a declared InterruptionSupport")
        if type(self.progress) is not bool:
            raise TypeError("Call progress declaration must be boolean")


@dataclass(frozen=True, slots=True)
class ImplementationDescription:
    binding_id: str
    binding_revision: str
    contract_version: int
    configuration: ConfigurationDescription
    assets: tuple[AssetDescription, ...]
    calls: tuple[CallDescription, ...]
    resident_resources: tuple[ResourceRequirement, ...]
    isolation: IsolationRequirement

    def __post_init__(self) -> None:
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")
        if not (type(self.contract_version) is int and 1 <= self.contract_version):
            raise ValueError("Contract version must be a positive integer")
        if type(self.configuration) is not ConfigurationDescription:
            raise TypeError("An implementation requires a ConfigurationDescription")
        _records(self.assets, AssetDescription, "Scientific assets")
        _unique(self.assets, lambda value: value.name, "Scientific assets")
        _records(self.calls, CallDescription, "Call descriptions")
        if not self.calls:
            raise ValueError("An implementation must declare at least one call")
        _unique(self.calls, lambda value: value.name, "Call descriptions")
        _records(self.resident_resources, ResourceRequirement, "Resident resource requirements")
        _unique(
            self.resident_resources,
            lambda value: value.identity,
            "Resident resource requirements",
        )
        if type(self.isolation) is not IsolationRequirement:
            raise TypeError("Implementation isolation must be a declared IsolationRequirement")


@dataclass(frozen=True, slots=True)
class CallRef:
    binding_id: str
    binding_revision: str
    name: str

    def __post_init__(self) -> None:
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")
        _identity(self.name, "Call name")


@dataclass(frozen=True, slots=True)
class InstanceHooks[InstanceT]:
    """Construct bookkeeping first; the host retains it before initialization."""

    construct: Callable[[], InstanceT] = field(repr=False)
    initialize: Callable[[InstanceT], None] = field(repr=False)
    dispose: Callable[[InstanceT], None] = field(repr=False)

    def __post_init__(self) -> None:
        _callback(self.construct, "Instance constructor")
        _callback(self.initialize, "Instance initializer")
        _callback(self.dispose, "Instance disposal")


@dataclass(frozen=True, slots=True)
class AnalysisHooks[InstanceT, AnalysisT]:
    """Own scientific analysis state before preparation necessarily succeeds."""

    construct: Callable[[InstanceT], AnalysisT] = field(repr=False)
    initialize: Callable[[AnalysisT], None] = field(repr=False)
    dispose: Callable[[AnalysisT], None] = field(repr=False)

    def __post_init__(self) -> None:
        _callback(self.construct, "Analysis constructor")
        _callback(self.initialize, "Analysis initializer")
        _callback(self.dispose, "Analysis disposal")


@dataclass(frozen=True, slots=True)
class ReturnedState[StateT]:
    name: str
    kind: str
    value: StateT = field(repr=False)
    dispose: Callable[[StateT], None] = field(repr=False)

    def __post_init__(self) -> None:
        _identity(self.name, "Returned state name")
        _identity(self.kind, "Returned scientific state kind")
        _callback(self.dispose, "Returned state disposal")


@dataclass(frozen=True, slots=True)
class ScientificReturn[OutputT]:
    """Retain this typed scientific result and its states before encoding."""

    value: OutputT = field(repr=False)
    states: tuple[ReturnedState[Any], ...]
    dispose_value: Callable[[OutputT], None] | None = field(repr=False)

    def __post_init__(self) -> None:
        _records(self.states, ReturnedState, "Returned scientific states")
        _unique(self.states, lambda value: value.name, "Returned scientific states")
        if self.dispose_value is not None:
            _callback(self.dispose_value, "Scientific return disposal")


@dataclass(frozen=True, slots=True)
class CallBinding[InstanceT, AnalysisT, InputT, StateT, OutputT]:
    """Preserve provider types across payload, state, science and result boundaries."""

    description: CallDescription
    input_codec: PayloadCodec[InputT] = field(repr=False)
    state_binding: StateBinding[StateT] = field(repr=False)
    scientific_call: Callable[
        [InstanceT, AnalysisT, InputT, StateT, ScientificControl],
        ScientificReturn[OutputT],
    ] = field(repr=False)
    result_codec: PayloadCodec[OutputT] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.description) is not CallDescription:
            raise TypeError("A call binding requires its CallDescription")
        for codec, label in (
            (self.input_codec, "Input codec"),
            (self.result_codec, "Result codec"),
        ):
            if not isinstance(codec, PayloadCodec):
                raise TypeError(f"{label} must implement PayloadCodec")
        if self.input_codec.format != self.description.input_format:
            raise ValueError("Input codec format differs from the call description")
        if self.result_codec.format != self.description.result_format:
            raise ValueError("Result codec format differs from the call description")
        if not isinstance(self.state_binding, StateBinding):
            raise TypeError("State binding must implement StateBinding")
        declared_states = tuple(parameter.name for parameter in self.description.state_inputs)
        if self.state_binding.names != declared_states:
            raise ValueError("State binding fields differ from the call's declared state inputs")
        _callback(self.scientific_call, "Scientific call")
        _require_scientific_signature(
            self.scientific_call,
            self.input_codec.value_type,
            self.state_binding.value_type,
            self.result_codec.value_type,
        )

    def decode_input(self, payload: bytes) -> InputT:
        """Decode the declared portable payload into the provider's InputT."""
        return self.input_codec.decode(payload)

    def invoke(
        self,
        instance: InstanceT,
        analysis: AnalysisT,
        scientific_input: InputT,
        live_states: Mapping[str, object],
        control: ScientificControl,
    ) -> ScientificReturn[OutputT]:
        """Bind live state to StateT immediately before the scientific call."""
        state = self.state_binding.bind(live_states)
        return self.scientific_call(
            instance,
            analysis,
            scientific_input,
            state,
            control,
        )

    def encode_result(self, value: OutputT) -> bytes:
        return self.result_codec.encode(value)


@dataclass(frozen=True, slots=True)
class ImplementationBinding[InstanceT, AnalysisT]:
    """Configured scientific code loaded only inside an authorized host."""

    description: ImplementationDescription
    instance: InstanceHooks[InstanceT] | None
    analysis: AnalysisHooks[InstanceT, AnalysisT] | None
    calls: tuple[CallBinding[InstanceT, AnalysisT, Any, Any, Any], ...]

    def __post_init__(self) -> None:
        if type(self.description) is not ImplementationDescription:
            raise TypeError("A binding requires its ImplementationDescription")
        if self.instance is not None and type(self.instance) is not InstanceHooks:
            raise TypeError("Instance hooks must be InstanceHooks or None")
        if self.analysis is not None and type(self.analysis) is not AnalysisHooks:
            raise TypeError("Analysis hooks must be AnalysisHooks or None")
        _records(self.calls, CallBinding, "Call bindings")
        bound = {call.description.name: call.description for call in self.calls}
        declared = {call.name: call for call in self.description.calls}
        if len(bound) != len(self.calls) or bound != declared:
            raise ValueError("Call bindings must agree exactly with the declared calls")


class ScientificRejection(CallRejected):
    """Reviewed scientific rejection after portable input admission."""


def description_document(description: ImplementationDescription) -> dict[str, object]:
    """Return the scheduler-facing, JSON-compatible view of one binding."""
    if type(description) is not ImplementationDescription:
        raise TypeError("Description export requires an ImplementationDescription")

    def resource_document(requirement: ResourceRequirement) -> dict[str, object]:
        return {
            "kind": requirement.kind,
            "amount": requirement.amount,
            "unit": requirement.unit,
            "selector": requirement.selector,
            "exclusive": requirement.exclusive,
        }

    return {
        "binding_id": description.binding_id,
        "binding_revision": description.binding_revision,
        "contract_version": description.contract_version,
        "configuration": {
            "format": description.configuration.format,
            "digest": description.configuration.digest,
        },
        "assets": [
            {
                "name": asset.name,
                "media_type": asset.media_type,
                "digest": asset.digest,
                "size_bytes": asset.size_bytes,
            }
            for asset in description.assets
        ],
        "resident_resources": [
            resource_document(requirement)
            for requirement in description.resident_resources
        ],
        "isolation": description.isolation.value,
        "calls": [
            {
                "name": call.name,
                "input_format": call.input_format,
                "result_format": call.result_format,
                "state_inputs": [
                    {"name": state.name, "kind": state.kind, "use": state.use.value}
                    for state in call.state_inputs
                ],
                "state_outputs": [
                    {"name": state.name, "kind": state.kind}
                    for state in call.state_outputs
                ],
                "resources": [
                    resource_document(requirement)
                    for requirement in call.resources
                ],
                "concurrency": call.concurrency.value,
                "interruption": call.interruption.value,
                "progress": call.progress,
            }
            for call in description.calls
        ],
    }
