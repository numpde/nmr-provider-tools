"""Durable, typed boundary implemented by scientific contributions.

Descriptions are immutable data that orchestration can inspect without importing
scientific dependencies. Bindings stay inside the scientific host and preserve
provider-owned Python types across decoding, state binding, science and encoding.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import inspect
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from types import NoneType
from typing import Any, Protocol, get_args, get_origin, get_type_hints, runtime_checkable

from provider_core.contribution_errors import ScientificRejection
from provider_core.payload import (
    DataclassStateBinding,
    JsonDataclassCodec,
    PayloadCodec,
    StateBinding,
)


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
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}\Z", value) is None:
        raise ValueError(f"{label} must be a lowercase sha256:<64 hex characters> digest")


def _binding_revision(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"(?:sha256:|configured-v2:sha256:)[0-9a-f]{64}\Z", value) is None:
        raise ValueError("Binding revision must be a lowercase sha256 or configured-v2:sha256 digest")


def _records(values: object, record_type: type[object], label: str) -> None:
    if not (
        type(values) is tuple
        and all(type(value) is record_type for value in values)
    ):
        raise TypeError(f"{label} must be an immutable tuple of {record_type.__name__} records")


def _unique(values: tuple[object, ...], key: Callable[[object], object], label: str) -> None:
    if len({key(value) for value in values}) != len(values):
        raise ValueError(f"{label} cannot contain duplicates")


def _callback(value: object, label: str) -> None:
    if not callable(value):
        raise TypeError(f"{label} must be callable")


@dataclass(frozen=True, slots=True)
class _ScientificCallShape:
    instance: type[object] | None
    analysis: type[object] | None
    input: type[object]
    state: type[object]
    output: type[object]


def _resolved_callable_hints(
    callable_: Callable[..., object],
    *,
    label: str,
) -> dict[str, object]:
    try:
        return get_type_hints(callable_, include_extras=True)
    except (NameError, TypeError) as error:
        raise TypeError(f"{label} annotations could not be resolved") from error


def _positional_parameters(
    callable_: Callable[..., object],
    *,
    count: int,
    label: str,
) -> tuple[inspect.Parameter, ...]:
    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{label} must expose an inspectable signature") from error

    parameters = tuple(signature.parameters.values())
    if len(parameters) != count or any(
        parameter.kind
        not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for parameter in parameters
    ):
        raise TypeError(f"{label} must accept exactly {count} positional parameters")
    return parameters


def _require_callable_signature(
    callable_: Callable[..., object],
    *,
    parameter_types: tuple[type[object], ...],
    return_type: type[object],
    label: str,
) -> None:
    parameters = _positional_parameters(
        callable_,
        count=len(parameter_types),
        label=label,
    )
    hints = _resolved_callable_hints(callable_, label=label)

    for (parameter, expected_type) in zip(parameters, parameter_types, strict=True):
        actual_type = hints.get(parameter.name)
        if actual_type is not expected_type:
            raise TypeError(
                f"{label} parameter {parameter.name!r} annotation must be exactly "
                f"{expected_type.__qualname__}"
            )

    if hints.get("return") is not return_type:
        raise TypeError(
            f"{label} return annotation must be exactly {return_type.__qualname__}"
        )


def _constructed_type(
    callable_: Callable[..., object],
    *,
    parameter_types: tuple[type[object], ...],
    label: str,
) -> type[object]:
    parameters = _positional_parameters(
        callable_,
        count=len(parameter_types),
        label=label,
    )
    hints = _resolved_callable_hints(callable_, label=label)

    for (parameter, expected_type) in zip(parameters, parameter_types, strict=True):
        if hints.get(parameter.name) is not expected_type:
            raise TypeError(
                f"{label} parameter {parameter.name!r} annotation must be exactly "
                f"{expected_type.__qualname__}"
            )

    constructed_type = hints.get("return")
    if not isinstance(constructed_type, type) or constructed_type is NoneType:
        raise TypeError(f"{label} must declare one concrete return type")
    return constructed_type


def _scientific_call_shape(
    scientific_call: Callable[..., object],
) -> _ScientificCallShape:
    if isinstance(scientific_call, _PlainCall):
        return _ScientificCallShape(
            instance=None, analysis=None, input=scientific_call.input_type,
            state=NoState, output=scientific_call.result_type,
        )
    parameters = _positional_parameters(scientific_call, count=1, label="Scientific call")
    resolved_annotations = _resolved_callable_hints(scientific_call, label="Scientific call")
    context_type = resolved_annotations.get(parameters[0].name)
    if get_origin(context_type) is not CallContext or len(get_args(context_type)) != 4:
        raise TypeError("Scientific call must accept one CallContext[Instance, Analysis, Input, State]")
    instance_type, analysis_type, input_type, state_type = get_args(context_type)
    if not all(isinstance(value, type) for value in (instance_type, analysis_type, input_type, state_type)):
        raise TypeError("Scientific context parameters must name concrete types")

    return_annotation = resolved_annotations.get("return")
    if get_origin(return_annotation) is not ScientificReturn or len(get_args(return_annotation)) != 1:
        raise TypeError("Scientific call return annotation must be ScientificReturn[OutputT]")
    (output_type,) = get_args(return_annotation)
    if not isinstance(output_type, type):
        raise TypeError("Scientific call output annotation must be a concrete type")

    return _ScientificCallShape(
        instance=instance_type,
        analysis=analysis_type,
        input=input_type,
        state=state_type,
        output=output_type,
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfiguredIdentity:
    """Own the advertised configuration/assets and their derived revision once.

    The implementation identity is an explicit code-identity assertion supplied
    by deployment. The explicitly versioned revision covers complete semantic
    configuration/asset descriptions. Legacy sha256-only references keep their
    old identity; no conversion or replay is inferred. Code is not discovered.
    """

    binding_id: str
    implementation: str
    contract_version: int
    configuration: ConfigurationDescription | None
    assets: tuple[AssetDescription, ...]
    revision: str = field(init=False)

    def __post_init__(self) -> None:
        _identity(self.binding_id, "Binding identity")
        _identity(self.implementation, "Implementation identity")
        if not self.implementation.isascii():
            raise ValueError("Implementation identity must be ASCII")
        if not (type(self.contract_version) is int and 0 < self.contract_version):
            raise ValueError("Contract version must be a positive integer")
        if self.configuration is not None and type(self.configuration) is not ConfigurationDescription:
            raise TypeError("Configuration must be a ConfigurationDescription or None")
        _records(self.assets, AssetDescription, "Scientific assets")
        _unique(self.assets, lambda asset: asset.name, "Scientific assets")
        configuration = None
        if self.configuration is not None:
            configuration = {"format": self.configuration.format, "digest": self.configuration.digest}
        document = {
            "schema_id": "provider-core.configured-identity.v2",
            "implementation": self.implementation,
            "contract_version": self.contract_version,
            "configuration": configuration,
            "assets": [
                {"name": asset.name, "media_type": asset.media_type,
                 "digest": asset.digest, "size_bytes": asset.size_bytes}
                for asset in self.assets
            ],
        }
        encoded = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        object.__setattr__(self, "revision", "configured-v2:sha256:" + hashlib.sha256(encoded).hexdigest())


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


@runtime_checkable
class DeclaredStateBinding[T](StateBinding[T], Protocol):
    """Own consumer state metadata and concrete input types together.

    Implementations must keep these declarations stable for the binding's
    lifetime; the built-in slot binding uses frozen input records.
    """

    @property
    def parameters(self) -> tuple[StateParameter, ...]: ...

    @property
    def input_types(self) -> Mapping[str, type[object]]: ...


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
class CallContext[InstanceT, AnalysisT, InputT, StateT]:
    """Scientific values for one invocation, without Jobs or host authority.

    Control is absent unless the call declares progress or cooperative stopping.
    Parameter names in the receiving Python function carry no protocol meaning.
    """

    instance: InstanceT
    analysis: AnalysisT
    scientific_input: InputT
    state: StateT
    control: ScientificControl | None

    def require_control(self) -> ScientificControl:
        """Obtain only the progress/stop capability this call actually declared."""
        if self.control is None:
            raise RuntimeError("This scientific call did not declare progress or cooperative stopping")
        return self.control


@dataclass(frozen=True, slots=True, kw_only=True)
class NoInput:
    """Portable marker for a call with no input fields."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NoState:
    """Host-bound marker for a call with no retained scientific state."""


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
    configuration: ConfigurationDescription | None
    assets: tuple[AssetDescription, ...]
    calls: tuple[CallDescription, ...]
    resident_resources: tuple[ResourceRequirement, ...]
    isolation: IsolationRequirement

    def __post_init__(self) -> None:
        _identity(self.binding_id, "Binding identity")
        _binding_revision(self.binding_revision)
        if not (type(self.contract_version) is int and 1 <= self.contract_version):
            raise ValueError("Contract version must be a positive integer")
        if (
            self.configuration is not None
            and type(self.configuration) is not ConfigurationDescription
        ):
            raise TypeError(
                "Implementation configuration must be a ConfigurationDescription "
                "or None"
            )
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
        _binding_revision(self.binding_revision)
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


@runtime_checkable
class StateOutputBinding[StateT](Protocol):
    """A host-local typed output slot projected to scheduler-only metadata.

    Its description and concrete type must stay stable for the binding's
    lifetime; the built-in output slot is a frozen record.
    """

    @property
    def description(self) -> StateOutput: ...

    @property
    def value_type(self) -> type[StateT]: ...


@dataclass(frozen=True, slots=True)
class ScientificReturn[OutputT]:
    """Retain this typed scientific result and its states before encoding."""

    value: OutputT = field(repr=False)
    states: tuple[ReturnedState[Any], ...] = ()
    dispose_value: Callable[[OutputT], None] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _records(self.states, ReturnedState, "Returned scientific states")
        _unique(self.states, lambda value: value.name, "Returned scientific states")
        if self.dispose_value is not None:
            _callback(self.dispose_value, "Scientific return disposal")


@dataclass(frozen=True, slots=True)
class _PlainCall[InputT, OutputT]:
    """Adapt a plain calculation without inventing scientific lifetimes/control."""

    function: Callable[[InputT], OutputT] = field(repr=False)
    input_type: type[InputT]
    result_type: type[OutputT]

    def __call__(self, context: CallContext[Any, Any, InputT, NoState]) -> ScientificReturn[OutputT]:
        return ScientificReturn(value=self.function(context.scientific_input))


@dataclass(frozen=True, slots=True)
class CallBinding[InputT, StateT, OutputT]:
    """Preserve provider types across payload, state, science and result boundaries."""

    description: CallDescription
    input_codec: PayloadCodec[InputT] = field(repr=False)
    state_binding: StateBinding[StateT] = field(repr=False)
    scientific_call: Callable[
        [CallContext[Any, Any, InputT, StateT]], ScientificReturn[OutputT]
    ] = field(repr=False)
    result_codec: PayloadCodec[OutputT] = field(repr=False)
    _shape: _ScientificCallShape = field(init=False, repr=False, compare=False)
    _owner: ImplementationBinding | None = field(default=None, init=False, repr=False, compare=False)
    _member_name: str | None = field(default=None, init=False, repr=False, compare=False)
    _output_slots: tuple[StateOutputBinding[Any], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if type(self.description) is not CallDescription:
            raise TypeError("A call binding requires its CallDescription")

        for (codec, label) in (
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

        # repo-check: allow-tuple-generator -- StateBinding.names requires an immutable ordered tuple matching the declared state fields.
        declared_state_names = tuple(
            parameter.name
            for parameter in self.description.state_inputs
        )
        if self.state_binding.names != declared_state_names:
            raise ValueError("State binding fields differ from the call's declared state inputs")

        if self.description.state_inputs:
            if not isinstance(self.state_binding, DeclaredStateBinding):
                raise TypeError("Stateful calls require a binding that owns typed state declarations")
            if self.state_binding.parameters != self.description.state_inputs:
                raise ValueError("State binding kind/use differs from the call's declared inputs")
            if set(self.state_binding.input_types) != set(declared_state_names):
                raise ValueError("State binding types must match the declared input names")

        _callback(self.scientific_call, "Scientific call")
        if type(self._output_slots) is not tuple:
            raise TypeError("Scientific output slots must be an immutable tuple")
        output_descriptions = []
        for slot in self._output_slots:
            if not isinstance(slot, StateOutputBinding) or not isinstance(slot.value_type, type):
                raise TypeError("Scientific output slots require a concrete Python value type")
            output_descriptions.append(slot.description)
        if tuple(output_descriptions) != self.description.state_outputs:
            raise ValueError("Scientific output slots must match the declared outputs")
        call_shape = _scientific_call_shape(self.scientific_call)
        for (actual_type, expected_type, role) in (
            (call_shape.input, self.input_codec.value_type, "input"),
            (call_shape.state, self.state_binding.value_type, "state"),
            (call_shape.output, self.result_codec.value_type, "output"),
        ):
            if actual_type is not expected_type:
                raise TypeError(
                    f"Scientific call {role} annotation must be exactly "
                    f"{expected_type.__qualname__}"
                )

        object.__setattr__(self, "_shape", call_shape)


    @classmethod
    def from_json_dataclasses(
        cls,
        *,
        name: str,
        input_type: type[InputT],
        state_type: type[StateT],
        result_type: type[OutputT],
        input_format: str | None = None,
        result_format: str,
        state_outputs: tuple[StateOutputBinding[Any], ...],
        resources: tuple[ResourceRequirement, ...],
        concurrency: ConcurrencyRequirement,
        interruption: InterruptionSupport,
        progress: bool,
        scientific_call: Callable[
            [CallContext[Any, Any, InputT, StateT]], ScientificReturn[OutputT]
        ],
        state_binding: DeclaredStateBinding[StateT] | None = None,
    ) -> CallBinding[InputT, StateT, OutputT]:
        """Connect concrete codecs and a typed context callback at construction."""

        if input_type is NoInput:
            if input_format is not None:
                raise ValueError(
                    "Omit input_format when the scientific call has no input"
                )
            admitted_input_format = "provider-core.no-input.v1"
        else:
            if input_format is None:
                raise ValueError(
                    "A scientific call with input requires input_format"
                )
            admitted_input_format = input_format

        if state_binding is None:
            if state_type is not NoState:
                raise TypeError("Stateful calls require an explicit typed state binding")
            state_parameters = ()
        else:
            state_parameters = state_binding.parameters

        input_codec = JsonDataclassCodec(
            format=admitted_input_format,
            value_type=input_type,
        )
        result_codec = JsonDataclassCodec(
            format=result_format,
            value_type=result_type,
        )
        output_descriptions = []
        for slot in state_outputs:
            output_descriptions.append(slot.description)

        return cls(
            description=CallDescription(
                name=name,
                input_format=input_codec.format,
                result_format=result_codec.format,
                state_inputs=state_parameters,
                state_outputs=tuple(output_descriptions),
                resources=resources,
                concurrency=concurrency,
                interruption=interruption,
                progress=progress,
            ),
            input_codec=input_codec,
            state_binding=DataclassStateBinding(value_type=state_type) if state_binding is None else state_binding,
            scientific_call=scientific_call,
            result_codec=result_codec,
            _output_slots=state_outputs,
        )

    @classmethod
    def from_function(
        cls,
        *,
        name: str,
        input_type: type[InputT],
        result_type: type[OutputT],
        input_format: str,
        result_format: str,
        function: Callable[[InputT], OutputT],
        resources: tuple[ResourceRequirement, ...],
        concurrency: ConcurrencyRequirement,
    ) -> CallBinding[InputT, NoState, OutputT]:
        """Bind an ordinary unary calculation with no state/control hooks."""
        _require_callable_signature(
            function, parameter_types=(input_type,), return_type=result_type,
            label="Plain scientific function",
        )
        return CallBinding.from_json_dataclasses(
            name=name, input_type=input_type, state_type=NoState, result_type=result_type,
            input_format=input_format, result_format=result_format,
            state_outputs=(), resources=resources,
            concurrency=concurrency, interruption=InterruptionSupport.NONE, progress=False,
            scientific_call=_PlainCall(function, input_type, result_type),
        )

    def encode_input(self, value: InputT) -> bytes:
        """Encode input using this exact call's codec."""
        return self.input_codec.encode(value)

    def decode_result(self, payload: bytes) -> OutputT:
        """Decode a result using this exact call's codec."""
        return self.result_codec.decode(payload)

    def decode_input(self, payload: bytes) -> InputT:
        """Decode the declared portable payload into the provider's InputT."""
        return self.input_codec.decode(payload)

    def invoke(
        self,
        instance: object,
        analysis: object,
        scientific_input: InputT,
        live_states: Mapping[str, object],
        control: ScientificControl,
    ) -> ScientificReturn[OutputT]:
        """Bind and pass only the scientific values this call declares."""
        scientific_control = None
        if self.description.progress or self.description.interruption is InterruptionSupport.COOPERATIVE:
            scientific_control = control
        return self.scientific_call(
            CallContext(
                instance=instance, analysis=analysis, scientific_input=scientific_input,
                state=self.state_binding.bind(live_states),
                control=scientific_control,
            )
        )

    def encode_result(self, value: OutputT) -> bytes:
        return self.result_codec.encode(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImplementationBinding[InstanceT, AnalysisT]:
    """Base for provider-owned frozen records with named typed call fields.

    Call fields are the single local inventory. Construction clones and anchors
    each declaration; sharing a declaration across configurations never mutates
    the original or another binding. Only the private host inventory erases types.
    """

    identity: ConfiguredIdentity
    instance: InstanceHooks[InstanceT] | None
    analysis: AnalysisHooks[InstanceT, AnalysisT] | None
    resident_resources: tuple[ResourceRequirement, ...]
    isolation: IsolationRequirement
    description: ImplementationDescription = field(init=False)
    _calls: tuple[CallBinding[Any, Any, Any], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.identity) is not ConfiguredIdentity:
            raise TypeError("A binding requires one ConfiguredIdentity")
        if self.instance is not None and type(self.instance) is not InstanceHooks:
            raise TypeError("Instance hooks must be InstanceHooks or None")
        if self.analysis is not None and type(self.analysis) is not AnalysisHooks:
            raise TypeError("Analysis hooks must be AnalysisHooks or None")
        bound_calls = []
        member_hints = get_type_hints(type(self), include_extras=True)
        for member in fields(self):
            if not member.init:
                continue
            declaration = getattr(self, member.name)
            annotation = member_hints.get(member.name)
            if get_origin(annotation) is not CallBinding:
                if type(declaration) is CallBinding:
                    raise TypeError(f"Call member {member.name!r} must declare its concrete CallBinding types")
                continue
            if type(declaration) is not CallBinding:
                raise TypeError(f"Declared call member {member.name!r} must contain a CallBinding")
            expected_types = get_args(annotation)
            actual_types = (
                declaration.input_codec.value_type,
                declaration.state_binding.value_type,
                declaration.result_codec.value_type,
            )
            if expected_types != actual_types:
                raise TypeError(f"Call member {member.name!r} codecs differ from its declared types")
            bound = replace(declaration)
            object.__setattr__(bound, "_owner", self)
            object.__setattr__(bound, "_member_name", member.name)
            object.__setattr__(self, member.name, bound)
            bound_calls.append(bound)
        calls = tuple(bound_calls)
        if not calls:
            raise ValueError("A binding must declare at least one named call field")
        _unique(calls, lambda call: call.description.name, "Call bindings")
        object.__setattr__(self, "_calls", calls)
        descriptions = []
        for call in calls:
            descriptions.append(call.description)
        object.__setattr__(self, "description", ImplementationDescription(
            binding_id=self.identity.binding_id, binding_revision=self.identity.revision,
            contract_version=self.identity.contract_version,
            configuration=self.identity.configuration, assets=self.identity.assets,
            calls=tuple(descriptions), resident_resources=self.resident_resources,
            isolation=self.isolation,
        ))

        if self.instance is None:
            instance_type = NoneType
        else:
            instance_type = _constructed_type(
                self.instance.construct,
                parameter_types=(),
                label="Instance constructor",
            )
            _require_callable_signature(
                self.instance.initialize,
                parameter_types=(instance_type,),
                return_type=NoneType,
                label="Instance initializer",
            )
            _require_callable_signature(
                self.instance.dispose,
                parameter_types=(instance_type,),
                return_type=NoneType,
                label="Instance disposer",
            )

        if self.analysis is None:
            analysis_type = NoneType
        else:
            analysis_type = _constructed_type(
                self.analysis.construct,
                parameter_types=(instance_type,),
                label="Analysis constructor",
            )
            _require_callable_signature(
                self.analysis.initialize,
                parameter_types=(analysis_type,),
                return_type=NoneType,
                label="Analysis initializer",
            )
            _require_callable_signature(
                self.analysis.dispose,
                parameter_types=(analysis_type,),
                return_type=NoneType,
                label="Analysis disposer",
            )

        # A wire kind has one concrete meaning inside this configured binding.
        # Catch declaration collisions before acquiring or consuming live state.
        state_types = {}
        for call in calls:
            declared_types = []
            for slot in call._output_slots:
                declared_types.append((slot.description.kind, slot.value_type))
            for parameter in call.description.state_inputs:
                declared_types.append((parameter.kind, call.state_binding.input_types[parameter.name]))
            for kind, value_type in declared_types:
                if kind in state_types and state_types[kind] is not value_type:
                    raise TypeError(f"Scientific state kind {kind!r} names different Python types")
                state_types[kind] = value_type

        for call in calls:
            call_shape = call._shape
            if call_shape.instance is not None and call_shape.instance is not instance_type:
                raise TypeError(
                    f"Call {call.description.name!r} instance annotation must be "
                    f"{instance_type.__qualname__}"
                )
            if call_shape.analysis is not None and call_shape.analysis is not analysis_type:
                raise TypeError(
                    f"Call {call.description.name!r} analysis annotation must be "
                    f"{analysis_type.__qualname__}"
                )

    def ref[InputT, StateT, OutputT](self, call: CallBinding[InputT, StateT, OutputT]) -> CallRef:
        """Reference one of this exact binding's members, rejecting siblings."""
        if call._owner is not self:
            raise ValueError("Call belongs to another configured binding")
        return ref(call)


def ref[InputT, StateT, OutputT](call: CallBinding[InputT, StateT, OutputT]) -> CallRef:
    """Project an actual owned member to its data-only execution identity."""
    owner = call._owner
    name = call._member_name
    if owner is None or name is None:
        raise ValueError("Cannot reference a call before a configured binding owns it")
    if getattr(owner, name) is not call or not any(member is call for member in owner._calls):
        raise ValueError("Call does not belong to its configured binding")
    return CallRef(owner.description.binding_id, owner.description.binding_revision, call.description.name)


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
        # silent-defaults: allow-contract -- The exported contract distinguishes no configuration (null) from an explicitly configured empty document.
        "configuration": (
            None
            if description.configuration is None
            else {
                "format": description.configuration.format,
                "digest": description.configuration.digest,
            }
        ),
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
