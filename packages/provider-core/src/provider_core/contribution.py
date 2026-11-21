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
import reprlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from types import NoneType
from typing import Any, Protocol, get_args, get_origin, get_type_hints, runtime_checkable

from provider_core.contribution_errors import ScientificRejection as ScientificRejection
from provider_core.tools import require_identity_text
from provider_core.type_requirements import require_instance, require_type

from provider_core.payload import (
    DataclassStateBinding,
    JsonDataclassCodec,
    PayloadCodec,
    StateBinding,
)


def _digest(digest_text: object, digest_role: str) -> None:
    require_type(digest_text, str)

    expected_digest_pattern = r"sha256:[0-9a-f]{64}\Z"

    if re.fullmatch(pattern=expected_digest_pattern, string=digest_text) is None:
        raise ValueError(f"{digest_role=}; {reprlib.repr(digest_text)=} should match {expected_digest_pattern=}")


def _binding_revision(binding_revision: object) -> None:
    require_type(binding_revision, str)

    expected_revision_pattern = r"(?:sha256:|configured-v[23]:sha256:)[0-9a-f]{64}\Z"

    if re.fullmatch(pattern=expected_revision_pattern, string=binding_revision) is None:
        raise ValueError(f"{reprlib.repr(binding_revision)=} should match {expected_revision_pattern=}")


def _records(records: object, record_type: type[object]) -> None:
    require_type(records, tuple)

    for record in records:
        require_type(record, record_type)


def _set_records(records: object, record_type: type[object]) -> None:
    """Require an immutable unordered inventory with records of the declared type."""

    require_type(records, frozenset)

    for record in records:
        require_type(record, record_type)


def _unique(records: Collection[object], key: Callable[[object], object], record_role: str) -> None:
    unique_key_count = len({key(record) for record in records})
    record_count = len(records)

    if not (unique_key_count == record_count):
        raise ValueError(f"{record_role=}; {unique_key_count=} should equal {record_count=}")


def _callback(callback: object, callback_role: str) -> None:
    if not callable(callback):
        raise TypeError(f"{callback_role=} should be callable; {type(callback)=}")


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
    callable_role: str,
) -> dict[str, object]:
    try:
        return get_type_hints(callable_, include_extras=True)
    except (NameError, TypeError) as error:
        raise TypeError(f"{callable_role=} should have resolvable annotations") from error


def _positional_parameters(
    callable_: Callable[..., object],
    *,
    count: int,
    callable_role: str,
) -> tuple[inspect.Parameter, ...]:
    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{callable_role=} should expose an inspectable signature") from error

    parameters = tuple(signature.parameters.values())

    if (
        (len(parameters) != count) or
        any(
            (
                (parameter.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ))
                for parameter in parameters
            ),
        )
    ):
        raise TypeError(f"{callable_role=} should accept exactly {count=} positional parameters")

    return parameters


def _require_callable_signature(
    callable_: Callable[..., object],
    *,
    parameter_types: tuple[type[object], ...],
    return_type: type[object],
    callable_role: str,
) -> None:
    parameters = _positional_parameters(
        callable_,
        count=len(parameter_types),
        callable_role=callable_role,
    )

    hints = _resolved_callable_hints(callable_, callable_role=callable_role)

    for (parameter, expected_type) in zip(parameters, parameter_types, strict=True):
        parameter_annotation = hints.get(parameter.name)

        if not (parameter_annotation is expected_type):
            raise TypeError(f"{callable_role=}; {parameter.name=}; {parameter_annotation=} ≠ {expected_type=}")

    return_annotation = hints.get("return")

    if not (return_annotation is (expected_type := return_type)):
        raise TypeError(f"{callable_role=}; {return_annotation=} ≠ {expected_type=}")


def _constructed_type(
    callable_: Callable[..., object],
    *,
    parameter_types: tuple[type[object], ...],
    callable_role: str,
) -> type[object]:
    parameters = _positional_parameters(
        callable_,
        count=len(parameter_types),
        callable_role=callable_role,
    )

    hints = _resolved_callable_hints(callable_, callable_role=callable_role)

    for (parameter, expected_type) in zip(parameters, parameter_types, strict=True):
        parameter_annotation = hints.get(parameter.name)

        if not (parameter_annotation is expected_type):
            raise TypeError(f"{callable_role=}; {parameter.name=}; {parameter_annotation=} ≠ {expected_type=}")

    constructed_type = hints.get("return")

    require_instance(constructed_type, type)

    if constructed_type is NoneType:
        raise TypeError(f"{callable_role=} should declare a concrete return type")

    return constructed_type


def _scientific_call_shape(
    scientific_call: Callable[..., object],
) -> _ScientificCallShape:
    if isinstance(scientific_call, _MethodCall):
        return scientific_call.shape

    if isinstance(scientific_call, _PlainCall):
        return _ScientificCallShape(
            instance=None,
            analysis=None,
            input=scientific_call.input_type,
            state=NoState,
            output=scientific_call.result_type,
        )

    parameters = _positional_parameters(scientific_call, count=1, callable_role="Scientific call")
    resolved_annotations = _resolved_callable_hints(scientific_call, callable_role="Scientific call")
    context_type = resolved_annotations.get(parameters[0].name)

    context_origin = get_origin(context_type)

    if not (context_origin is (expected_context_origin := CallContext)):
        raise TypeError(f"{reprlib.repr(context_type)=} should originate from {expected_context_origin=}")

    context_arguments = get_args(context_type)

    if not (len(context_arguments) == (expected_context_parameters := 4)):
        raise TypeError(f"{reprlib.repr(context_type)=}; {len(context_arguments)=} ≠ {expected_context_parameters=}")

    (instance_type, analysis_type, input_type, state_type) = context_arguments

    require_instance(instance_type, type)

    require_instance(analysis_type, type)

    require_instance(input_type, type)

    require_instance(state_type, type)

    return_annotation = resolved_annotations.get("return")

    return_origin = get_origin(return_annotation)

    if not (return_origin is (expected_return_origin := ScientificReturn)):
        raise TypeError(f"{reprlib.repr(return_annotation)=} should originate from {expected_return_origin=}")

    return_arguments = get_args(return_annotation)

    if not (len(return_arguments) == (expected_return_parameters := 1)):
        raise TypeError(f"{reprlib.repr(return_annotation)=}; {len(return_arguments)=} ≠ {expected_return_parameters=}")

    (output_type,) = return_arguments

    require_instance(output_type, type)

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
        require_identity_text(self.format, "Configuration format")
        _digest(self.digest, "Configuration digest")


@dataclass(frozen=True, slots=True)
class AssetDescription:
    name: str
    media_type: str
    digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        require_identity_text(self.name, "Asset name")
        require_identity_text(self.media_type, "Asset media type")
        _digest(self.digest, "Asset digest")

        require_type(self.size_bytes, int)

        if not (0 < self.size_bytes):
            raise ValueError(f"{self.size_bytes=} should be positive")


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
    assets: frozenset[AssetDescription]
    revision: str = field(init=False)

    def __post_init__(self) -> None:
        require_identity_text(self.binding_id, "Binding identity")
        require_identity_text(self.implementation, "Implementation identity")

        if not self.implementation.isascii():
            raise ValueError(f"{reprlib.repr(self.implementation)=} should be ASCII")

        require_type(self.contract_version, int)

        if not (0 < self.contract_version):
            raise ValueError(f"{self.contract_version=} should be positive")

        if self.configuration is not None:
            actual_configuration = self.configuration

            require_type(actual_configuration, ConfigurationDescription)

        _set_records(records=self.assets, record_type=AssetDescription)
        _unique(self.assets, lambda asset: asset.name, "Scientific assets")
        configuration = None

        if self.configuration is not None:
            configuration = {"format": self.configuration.format, "digest": self.configuration.digest}

        configured_identity_fields = {
            "schema_id": "provider-core.configured-identity.v2",
            "implementation": self.implementation,
            "contract_version": self.contract_version,
            "configuration": configuration,
            "assets": [
                {
                    "name": asset.name,
                    "media_type": asset.media_type,
                    "digest": asset.digest,
                    "size_bytes": asset.size_bytes
                }
                for asset in sorted(self.assets, key=lambda asset: asset.name)
            ],
        }

        encoded = json.dumps(configured_identity_fields, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")

        object.__setattr__(self, "revision", "configured-v2:sha256:" + hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    """One resource need: its kind, quantity, unit and selection constraints.

    A frozenset of these records describes all needs that must be met together;
    for example, CPU threads, host memory and GPU memory for one model. Order
    has no meaning and repeating an identical need adds nothing. Two records
    with the same kind, unit and selector cannot specify conflicting quantities
    or exclusivity. These declarations do not allocate or prove availability.
    """

    kind: str
    amount: int
    unit: str
    selector: str | None = None
    exclusive: bool = False

    def __post_init__(self) -> None:
        require_identity_text(self.kind, "Resource kind")
        require_identity_text(self.unit, "Resource unit")

        require_type(self.amount, int)

        if not (0 < self.amount):
            raise ValueError(f"{reprlib.repr(self.kind)=}, {reprlib.repr(self.unit)=}; {self.amount=} should be positive")

        if self.selector is not None:
            require_identity_text(self.selector, "Resource selector")

        require_type(self.exclusive, bool)

    @property
    def identity(self) -> tuple[str, str, str | None]:
        return (self.kind, self.unit, self.selector)


def _require_resource_requirements(resource_requirements: object) -> None:
    """Require unordered immutable needs with one quantity per resource identity."""

    require_type(resource_requirements, frozenset)

    for resource_requirement in resource_requirements:
        require_type(resource_requirement, ResourceRequirement)

    _unique(
        records=resource_requirements,
        key=lambda resource_requirement: resource_requirement.identity,
        record_role="Resource requirements",
    )


@dataclass(frozen=True, slots=True)
class StateParameter:
    name: str
    kind: str
    use: StateUse

    def __post_init__(self) -> None:
        require_identity_text(self.name, "State parameter name")
        require_identity_text(self.kind, "Scientific state kind")

        require_type(self.use, StateUse)


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
        require_identity_text(self.name, "Output state name")
        require_identity_text(self.kind, "Output scientific state kind")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Bounded, provider-reviewed progress fact with no control authority."""

    stage: str
    completed: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        require_identity_text(self.stage, "Progress stage")

        if not ((self.completed is None) == (self.total is None)):
            raise ValueError(f"{self.completed=} and {self.total=} should be supplied together")

        if self.completed is not None:
            require_type(self.completed, int)

            require_type(self.total, int)

            if not (0 <= self.completed):
                raise ValueError(f"{self.completed=} should be nonnegative")

            if not (self.completed <= self.total):
                raise ValueError(f"{self.completed=} should not exceed {self.total=}")

            if not (0 < self.total):
                raise ValueError(f"{self.total=} should be positive")


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

        if not (self.control is not None):
            raise RuntimeError(f"{self.control=} should be available for progress or cooperative stopping")

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
    resources: frozenset[ResourceRequirement]
    concurrency: ConcurrencyRequirement
    interruption: InterruptionSupport
    progress: bool

    def __post_init__(self) -> None:
        require_identity_text(self.name, "Call name")
        require_identity_text(self.input_format, "Input format")
        require_identity_text(self.result_format, "Result format")
        _records(self.state_inputs, StateParameter)
        _unique(self.state_inputs, lambda state_parameter: state_parameter.name, "State parameters")
        _records(self.state_outputs, StateOutput)
        _unique(self.state_outputs, lambda state_output: state_output.name, "State outputs")
        _require_resource_requirements(resource_requirements=self.resources)

        require_type(self.concurrency, ConcurrencyRequirement)

        require_type(self.interruption, InterruptionSupport)

        require_type(self.progress, bool)


@dataclass(frozen=True, slots=True)
class ImplementationDescription:
    binding_id: str
    binding_revision: str
    contract_version: int
    configuration: ConfigurationDescription | None
    assets: frozenset[AssetDescription]
    calls: frozenset[CallDescription]
    resident_resources: frozenset[ResourceRequirement]
    isolation: IsolationRequirement

    def __post_init__(self) -> None:
        require_identity_text(self.binding_id, "Binding identity")
        _binding_revision(self.binding_revision)

        require_type(self.contract_version, int)

        if not (1 <= self.contract_version):
            raise ValueError(f"{self.contract_version=} should be positive")

        if self.configuration is not None:
            actual_configuration = self.configuration

            require_type(actual_configuration, ConfigurationDescription)

        _set_records(records=self.assets, record_type=AssetDescription)
        _unique(self.assets, lambda asset: asset.name, "Scientific assets")
        _set_records(records=self.calls, record_type=CallDescription)

        if not (self.calls):
            raise ValueError(f"{self.calls=} should not be empty")

        _unique(self.calls, lambda call_description: call_description.name, "Call descriptions")
        _require_resource_requirements(resource_requirements=self.resident_resources)

        require_type(self.isolation, IsolationRequirement)


@dataclass(frozen=True, slots=True)
class CallRef:
    binding_id: str
    binding_revision: str
    name: str

    def __post_init__(self) -> None:
        require_identity_text(self.binding_id, "Binding identity")
        _binding_revision(self.binding_revision)
        require_identity_text(self.name, "Call name")


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
        require_identity_text(self.name, "Returned state name")
        require_identity_text(self.kind, "Returned scientific state kind")
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
        _records(self.states, ReturnedState)
        _unique(self.states, lambda returned_state: returned_state.name, "Returned scientific states")

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


@dataclass(frozen=True, slots=True, kw_only=True)
class _MethodCall:
    """Compiled keyword arguments for an explicitly exported instance method.

    The stored function is the registered member, so later attribute replacement
    cannot silently change a registration's implementation. Stateful returns pass
    through untouched for the execution owner's custody-before-validation path.
    """

    function: Callable[..., object]
    receiver: str
    shape: _ScientificCallShape
    input_parameter: str | None
    analysis_parameter: str | None
    control_parameter: str | None
    wrapped_result: bool

    def __call__(self, *, context: CallContext) -> ScientificReturn:
        arguments = {self.receiver: context.instance}

        if self.input_parameter is not None:
            arguments[self.input_parameter] = context.scientific_input

        if self.analysis_parameter is not None:
            arguments[self.analysis_parameter] = context.analysis

        if self.control_parameter is not None:
            arguments[self.control_parameter] = context.control

        arguments.update(context.state)
        scientific_output = self.function(**arguments)

        if self.wrapped_result:
            return scientific_output

        return ScientificReturn(value=scientific_output)


class _CallOwner(Protocol):
    """One immutable configured inventory owns each executable call."""

    @property
    def description(self) -> ImplementationDescription: ...

    @property
    def _calls(self) -> tuple[CallBinding, ...]: ...


@dataclass(frozen=True, slots=True)
class CallBinding[InputT, StateT, OutputT]:
    """Preserve provider types across payload, state, science and result boundaries."""

    description: CallDescription
    input_codec: PayloadCodec[InputT] = field(repr=False)
    state_binding: StateBinding[StateT] = field(repr=False)

    scientific_call: Callable[
        [CallContext[Any, Any, InputT, StateT]],
        ScientificReturn[OutputT]
    ] = field(repr=False)

    result_codec: PayloadCodec[OutputT] = field(repr=False)
    _shape: _ScientificCallShape = field(init=False, repr=False, compare=False)
    _owner: _CallOwner | None = field(default=None, init=False, repr=False, compare=False)
    _output_slots: tuple[StateOutputBinding[Any], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        require_type(self.description, CallDescription)

        (input_codec, result_codec) = (self.input_codec, self.result_codec)

        require_instance(input_codec, PayloadCodec)
        require_instance(result_codec, PayloadCodec)

        input_codec_format = self.input_codec.format

        if not (input_codec_format == self.description.input_format):
            raise ValueError(f"{reprlib.repr(input_codec_format)=} ≠ {reprlib.repr(self.description.input_format)=}")

        result_codec_format = self.result_codec.format

        if not (result_codec_format == self.description.result_format):
            raise ValueError(f"{reprlib.repr(result_codec_format)=} ≠ {reprlib.repr(self.description.result_format)=}")

        require_instance(self.state_binding, StateBinding)

        # repo-check: allow-tuple-generator -- StateBinding.names requires an immutable ordered tuple matching the declared state fields.
        declared_state_names = tuple(
            parameter.name
            for parameter in self.description.state_inputs
        )

        bound_state_names = self.state_binding.names

        if not (bound_state_names == declared_state_names):
            raise ValueError(f"{reprlib.repr(bound_state_names)=} ≠ {reprlib.repr(declared_state_names)=}")

        if self.description.state_inputs:
            require_instance(self.state_binding, DeclaredStateBinding)

            bound_state_parameters = self.state_binding.parameters

            if not (bound_state_parameters == self.description.state_inputs):
                raise ValueError(f"{bound_state_parameters=} ≠ {self.description.state_inputs=}")

            bound_input_type_names = set(self.state_binding.input_types)
            declared_input_type_names = set(declared_state_names)

            if not (bound_input_type_names == declared_input_type_names):
                raise ValueError(f"{reprlib.repr(bound_input_type_names)=} ≠ {reprlib.repr(declared_input_type_names)=}")

        _callback(self.scientific_call, "Scientific call")

        require_type(self._output_slots, tuple)

        output_descriptions = []

        for slot in self._output_slots:
            require_instance(slot, StateOutputBinding)

            require_instance(slot.value_type, type)

            output_descriptions.append(slot.description)

        bound_output_descriptions = tuple(output_descriptions)

        if not (bound_output_descriptions == self.description.state_outputs):
            raise ValueError(f"{bound_output_descriptions=} ≠ {self.description.state_outputs=}")

        call_shape = _scientific_call_shape(self.scientific_call)

        for (declared_type, expected_type, role) in (
            (call_shape.input, self.input_codec.value_type, "input"),
            (call_shape.state, self.state_binding.value_type, "state"),
            (call_shape.output, self.result_codec.value_type, "output"),
        ):
            if not (declared_type is expected_type):
                raise TypeError(f"{role=}; {declared_type=} ≠ {expected_type=}")

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
        resources: frozenset[ResourceRequirement],
        concurrency: ConcurrencyRequirement,
        interruption: InterruptionSupport,
        progress: bool,
        scientific_call: Callable[
            [CallContext[Any, Any, InputT, StateT]],
            ScientificReturn[OutputT]
        ],
        state_binding: DeclaredStateBinding[StateT] | None = None,
    ) -> CallBinding[InputT, StateT, OutputT]:
        """Connect concrete codecs and a typed context callback at construction."""

        if input_type is NoInput:
            require_type(input_format, NoneType, error_factory=ValueError)

            admitted_input_format = "provider-core.no-input.v1"
        else:
            if not (input_format is not None):
                raise ValueError(f"{input_format=} should identify the input codec format")

            admitted_input_format = input_format

        if state_binding is None:
            if not (state_type is (expected_state_type := NoState)):
                raise TypeError(f"{state_type=} should be {expected_state_type=} when {state_binding=}")

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
        resources: frozenset[ResourceRequirement],
        concurrency: ConcurrencyRequirement,
    ) -> CallBinding[InputT, NoState, OutputT]:
        """Bind an ordinary unary calculation with no state/control hooks."""

        _require_callable_signature(
            function,
            parameter_types=(input_type,),
            return_type=result_type,
            callable_role="Plain scientific function",
        )

        return CallBinding.from_json_dataclasses(
            name=name,
            input_type=input_type,
            state_type=NoState,
            result_type=result_type,
            input_format=input_format,
            result_format=result_format,
            state_outputs=(),
            resources=resources,
            concurrency=concurrency,
            interruption=InterruptionSupport.NONE,
            progress=False,
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

        if self.description.progress or (self.description.interruption is InterruptionSupport.COOPERATIVE):
            scientific_control = control

        context = CallContext(
            instance=instance,
            analysis=analysis,
            scientific_input=scientific_input,
            state=self.state_binding.bind(live_states),
            control=scientific_control,
        )

        if isinstance(self.scientific_call, _MethodCall):
            return self.scientific_call(context=context)

        return self.scientific_call(context)

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
    resident_resources: frozenset[ResourceRequirement]
    isolation: IsolationRequirement
    description: ImplementationDescription = field(init=False)
    _calls: tuple[CallBinding[Any, Any, Any], ...] = field(init=False, repr=False, compare=False)

    def construct_instance(self) -> InstanceT | None:
        if self.instance is None:
            return None

        return self.instance.construct()

    def initialize_instance(self, *, instance: InstanceT, assets: object) -> None:
        if self.instance is not None:
            self.instance.initialize(instance)

    def close_instance(self, *, instance: InstanceT) -> None:
        if self.instance is not None:
            self.instance.dispose(instance)

    def construct_analysis(self, *, instance: InstanceT) -> AnalysisT | None:
        if self.analysis is None:
            return None

        return self.analysis.construct(instance)

    def initialize_analysis(self, *, analysis: AnalysisT) -> None:
        if self.analysis is not None:
            self.analysis.initialize(analysis)

    def close_analysis(self, *, analysis: AnalysisT) -> None:
        if self.analysis is not None:
            self.analysis.dispose(analysis)

    def __post_init__(self) -> None:
        require_type(self.identity, ConfiguredIdentity)

        if self.instance is not None:
            actual_instance = self.instance

            require_type(actual_instance, InstanceHooks)

        if self.analysis is not None:
            actual_analysis = self.analysis

            require_type(actual_analysis, AnalysisHooks)

        bound_calls = []
        member_hints = get_type_hints(type(self), include_extras=True)

        for member in fields(self):
            if not member.init:
                continue

            declaration = getattr(self, member.name)
            annotation = member_hints.get(member.name)

            if get_origin(annotation) is not CallBinding:
                # repo-check: retain-type-guard -- Reject a concrete CallBinding field unless its generic port types are declared
                if type(declaration) is CallBinding:
                    raise TypeError(f"{member.name=}; {reprlib.repr(annotation)=} should specify input, state and output types")

                continue

            require_type(declaration, CallBinding)

            expected_types = get_args(annotation)

            actual_types = (
                declaration.input_codec.value_type,
                declaration.state_binding.value_type,
                declaration.result_codec.value_type,
            )

            if not (expected_types == actual_types):
                raise TypeError(f"{member.name=}; {actual_types=} ≠ {expected_types=}")

            bound = replace(declaration)

            object.__setattr__(bound, "_owner", self)
            object.__setattr__(self, member.name, bound)
            bound_calls.append(bound)

        calls = tuple(bound_calls)

        if not (calls):
            raise ValueError(f"{calls=} should not be empty for {reprlib.repr(self.identity.binding_id)=}")

        _unique(calls, lambda call: call.description.name, "Call bindings")
        object.__setattr__(self, "_calls", calls)
        descriptions = []

        for call in calls:
            descriptions.append(call.description)

        object.__setattr__(
            self,
            "description",
            ImplementationDescription(
                binding_id=self.identity.binding_id,
                binding_revision=self.identity.revision,
                contract_version=self.identity.contract_version,
                configuration=self.identity.configuration,
                assets=self.identity.assets,
                calls=frozenset(descriptions),
                resident_resources=self.resident_resources,
                isolation=self.isolation,
            )
        )

        if self.instance is None:
            instance_type = NoneType
        else:
            instance_type = _constructed_type(
                self.instance.construct,
                parameter_types=(),
                callable_role="Instance constructor",
            )

            _require_callable_signature(
                self.instance.initialize,
                parameter_types=(instance_type,),
                return_type=NoneType,
                callable_role="Instance initializer",
            )

            _require_callable_signature(
                self.instance.dispose,
                parameter_types=(instance_type,),
                return_type=NoneType,
                callable_role="Instance disposer",
            )

        if self.analysis is None:
            analysis_type = NoneType
        else:
            analysis_type = _constructed_type(
                self.analysis.construct,
                parameter_types=(instance_type,),
                callable_role="Analysis constructor",
            )

            _require_callable_signature(
                self.analysis.initialize,
                parameter_types=(analysis_type,),
                return_type=NoneType,
                callable_role="Analysis initializer",
            )

            _require_callable_signature(
                self.analysis.dispose,
                parameter_types=(analysis_type,),
                return_type=NoneType,
                callable_role="Analysis disposer",
            )

        _require_state_kinds(calls=calls)

        for call in calls:
            call_shape = call._shape

            if not ((call_shape.instance is None) or (call_shape.instance is instance_type)):
                raise TypeError(f"{call.description.name=}; {call_shape.instance=} ≠ {instance_type=}")

            if not ((call_shape.analysis is None) or (call_shape.analysis is analysis_type)):
                raise TypeError(f"{call.description.name=}; {call_shape.analysis=} ≠ {analysis_type=}")

    def ref[InputT, StateT, OutputT](self, call: CallBinding[InputT, StateT, OutputT]) -> CallRef:
        """Reference one of this exact binding's members, rejecting siblings."""

        if not (call._owner is self):
            raise ValueError(f"{reprlib.repr(call.description.name)=}; {id(call._owner)=} ≠ {id(self)=}")

        return ref(call)


def _require_state_kinds(*, calls: tuple[CallBinding, ...]) -> None:
    """A scientific kind has one concrete type within a configured implementation."""
    state_types = {}

    for call in calls:
        declared_types = []

        for slot in call._output_slots:
            declared_types.append((slot.description.kind, slot.value_type))

        for parameter in call.description.state_inputs:
            declared_types.append((parameter.kind, call.state_binding.input_types[parameter.name]))

        for (kind, value_type) in declared_types:
            if kind in state_types:
                expected_state_type = state_types[kind]

                if not (value_type is expected_state_type):
                    raise TypeError(f"{kind=}; {value_type=} ≠ {expected_state_type=}")

            state_types[kind] = value_type


def ref[InputT, StateT, OutputT](call: CallBinding[InputT, StateT, OutputT]) -> CallRef:
    """Project an actual owned member to its data-only execution identity."""
    call_owner = call._owner

    if not (call_owner is not None):
        raise ValueError(f"{call_owner=} should identify a configured binding before referencing {reprlib.repr(call.description.name)=}")

    if not any((member is call) for member in call_owner._calls):
        owned_member_ids = {id(member) for member in call_owner._calls}

        raise ValueError(f"{id(call)=} should be one of {owned_member_ids=}")

    return CallRef(call_owner.description.binding_id, call_owner.description.binding_revision, call.description.name)


def description_document(description: ImplementationDescription) -> dict[str, object]:
    """Return the scheduler-facing, JSON-compatible view of one binding."""

    require_type(description, ImplementationDescription)

    # Requirements are unordered; canonical array order keeps exported revisions stable.
    # Compare selector presence before its value so None never compares with a string.
    def resource_order(requirement: ResourceRequirement):
        return (requirement.kind, requirement.unit, requirement.selector is not None, requirement.selector)

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
            for asset in sorted(description.assets, key=lambda asset: asset.name)
        ],
        "resident_resources": [
            resource_document(requirement)
            for requirement in sorted(description.resident_resources, key=resource_order)
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
                    for requirement in sorted(call.resources, key=resource_order)
                ],
                "concurrency": call.concurrency.value,
                "interruption": call.interruption.value,
                "progress": call.progress,
            }
            for call in sorted(description.calls, key=lambda call: call.name)
        ],
    }
