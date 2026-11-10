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
from types import NoneType
from typing import Any, Protocol, cast, get_args, get_origin, get_type_hints

from provider_core.contribution_errors import ScientificRejection
from provider_core.payload import (
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


@dataclass(frozen=True, slots=True)
class _ScientificCallShape:
    parameters: tuple[str, ...]
    instance: type[object] | None
    analysis: type[object] | None
    input: type[object]
    state: type[object]
    output: type[object]
    control: bool


_NO_INPUT_FORMAT = "provider-core.no-input.v1"


_CALL_PARAMETERS = (
    "instance",
    "analysis",
    "scientific_input",
    "state",
    "control",
)


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
    try:
        signature = inspect.signature(scientific_call)
    except (TypeError, ValueError) as error:
        raise TypeError("Scientific call must expose an inspectable signature") from error

    parameters = tuple(signature.parameters.values())
    if any(
        parameter.kind is not inspect.Parameter.KEYWORD_ONLY
        or parameter.default is not inspect.Parameter.empty
        for parameter in parameters
    ):
        raise TypeError(
            "Scientific call parameters must be required keyword-only parameters"
        )

    names = tuple(parameter.name for parameter in parameters)
    unknown = tuple(name for name in names if name not in _CALL_PARAMETERS)
    if unknown:
        raise TypeError(f"Scientific call has unknown parameters: {unknown!r}")
    canonical_names = tuple(name for name in _CALL_PARAMETERS if name in names)
    if names != canonical_names:
        raise TypeError(
            "Scientific call parameters must follow instance, analysis, "
            "scientific_input, state, control order"
        )

    hints = _resolved_callable_hints(scientific_call, label="Scientific call")
    annotations = {}
    for parameter in parameters:
        annotation = hints.get(parameter.name)
        if parameter.name == "control":
            if annotation is not ScientificControl:
                raise TypeError(
                    "Scientific call control annotation must be ScientificControl"
                )
            continue
        if not isinstance(annotation, type):
            raise TypeError(
                f"Scientific call parameter {parameter.name!r} annotation must be "
                "a concrete type"
            )
        annotations[parameter.name] = annotation

    instance_type = annotations.get("instance")
    analysis_type = annotations.get("analysis")
    input_type = annotations.get("scientific_input", NoInput)
    state_type = annotations.get("state", NoState)
    if instance_type is NoneType:
        raise TypeError("Omit the instance parameter when a call has no instance")
    if analysis_type is NoneType:
        raise TypeError("Omit the analysis parameter when a call has no analysis")
    if "scientific_input" in names and input_type is NoInput:
        raise TypeError("Omit scientific_input when a call has no input fields")
    if "state" in names and state_type is NoState:
        raise TypeError("Omit state when a call has no retained scientific state")

    returned = hints.get("return")
    if get_origin(returned) is not ScientificReturn or len(get_args(returned)) != 1:
        raise TypeError("Scientific call return annotation must be ScientificReturn[OutputT]")
    (output_type,) = get_args(returned)
    if not isinstance(output_type, type):
        raise TypeError("Scientific call output annotation must be a concrete type")

    return _ScientificCallShape(
        parameters=names,
        instance=instance_type,
        analysis=analysis_type,
        input=input_type,
        state=state_type,
        output=output_type,
        control="control" in names,
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
        _digest(self.binding_revision, "Binding revision")
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
class CallBinding[InputT, StateT, OutputT]:
    """Preserve provider types across payload, state, science and result boundaries."""

    description: CallDescription
    input_codec: PayloadCodec[InputT] = field(repr=False)
    state_binding: StateBinding[StateT] = field(repr=False)
    scientific_call: Callable[..., ScientificReturn[OutputT]] = field(repr=False)
    result_codec: PayloadCodec[OutputT] = field(repr=False)
    _shape: _ScientificCallShape = field(init=False, repr=False, compare=False)

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

        declared_states = tuple(
            parameter.name
            for parameter in self.description.state_inputs
        )
        if self.state_binding.names != declared_states:
            raise ValueError("State binding fields differ from the call's declared state inputs")

        _callback(self.scientific_call, "Scientific call")
        shape = _scientific_call_shape(self.scientific_call)
        for (actual, expected, label) in (
            (shape.input, self.input_codec.value_type, "input"),
            (shape.state, self.state_binding.value_type, "state"),
            (shape.output, self.result_codec.value_type, "output"),
        ):
            if actual is not expected:
                raise TypeError(
                    f"Scientific call {label} annotation must be exactly "
                    f"{expected.__qualname__}"
                )
        needs_control = (
            self.description.interruption is InterruptionSupport.COOPERATIVE
            or self.description.progress
        )
        if shape.control != needs_control:
            raise TypeError(
                "Scientific call control parameter must match its interruption "
                "and progress declarations"
            )
        object.__setattr__(self, "_shape", shape)

    @classmethod
    def from_json_dataclasses(
        cls,
        *,
        name: str,
        input_format: str | None = None,
        result_format: str,
        state_inputs: tuple[StateParameter, ...],
        state_outputs: tuple[StateOutput, ...],
        resources: tuple[ResourceRequirement, ...],
        concurrency: ConcurrencyRequirement,
        interruption: InterruptionSupport,
        progress: bool,
        scientific_call: Callable[..., ScientificReturn[OutputT]],
    ) -> CallBinding[InputT, StateT, OutputT]:
        """Build one typed JSON call from its scientific function and metadata."""
        shape = _scientific_call_shape(scientific_call)
        input_type = cast(type[InputT], shape.input)
        state_type = cast(type[StateT], shape.state)
        output_type = cast(type[OutputT], shape.output)

        if input_type is NoInput:
            if input_format is not None:
                raise ValueError(
                    "Omit input_format when the scientific call has no input"
                )
            admitted_input_format = _NO_INPUT_FORMAT
        else:
            if input_format is None:
                raise ValueError(
                    "A scientific call with input requires input_format"
                )
            admitted_input_format = input_format

        input_codec = JsonDataclassCodec(
            format=admitted_input_format,
            value_type=input_type,
        )
        result_codec = JsonDataclassCodec(
            format=result_format,
            value_type=output_type,
        )

        return cls(
            description=CallDescription(
                name=name,
                input_format=input_codec.format,
                result_format=result_codec.format,
                state_inputs=state_inputs,
                state_outputs=state_outputs,
                resources=resources,
                concurrency=concurrency,
                interruption=interruption,
                progress=progress,
            ),
            input_codec=input_codec,
            state_binding=DataclassStateBinding(value_type=state_type),
            scientific_call=scientific_call,
            result_codec=result_codec,
        )

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
        available = {
            "instance": instance,
            "analysis": analysis,
            "scientific_input": scientific_input,
            "state": self.state_binding.bind(live_states),
            "control": control,
        }
        return self.scientific_call(
            **{name: available[name] for name in self._shape.parameters}
        )

    def encode_result(self, value: OutputT) -> bytes:
        return self.result_codec.encode(value)


@dataclass(frozen=True, slots=True)
class ImplementationBinding[InstanceT, AnalysisT]:
    """Configured scientific code loaded only inside an authorized host."""

    description: ImplementationDescription
    instance: InstanceHooks[InstanceT] | None
    analysis: AnalysisHooks[InstanceT, AnalysisT] | None
    calls: tuple[CallBinding[Any, Any, Any], ...]

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

        for call in self.calls:
            shape = call._shape
            if shape.instance is not None and shape.instance is not instance_type:
                raise TypeError(
                    f"Call {call.description.name!r} instance annotation must be "
                    f"{instance_type.__qualname__}"
                )
            if shape.analysis is not None and shape.analysis is not analysis_type:
                raise TypeError(
                    f"Call {call.description.name!r} analysis annotation must be "
                    f"{analysis_type.__qualname__}"
                )

    @classmethod
    def from_calls(
        cls,
        *,
        binding_id: str,
        binding_revision: str,
        contract_version: int,
        configuration: ConfigurationDescription | None,
        assets: tuple[AssetDescription, ...],
        calls: tuple[CallBinding[Any, Any, Any], ...],
        resident_resources: tuple[ResourceRequirement, ...],
        isolation: IsolationRequirement,
        instance: InstanceHooks[InstanceT] | None,
        analysis: AnalysisHooks[InstanceT, AnalysisT] | None,
    ) -> ImplementationBinding[InstanceT, AnalysisT]:
        """Derive the scheduler call descriptions from the bound calls."""
        return cls(
            description=ImplementationDescription(
                binding_id=binding_id,
                binding_revision=binding_revision,
                contract_version=contract_version,
                configuration=configuration,
                assets=assets,
                calls=tuple(call.description for call in calls),
                resident_resources=resident_resources,
                isolation=isolation,
            ),
            instance=instance,
            analysis=analysis,
            calls=calls,
        )


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
