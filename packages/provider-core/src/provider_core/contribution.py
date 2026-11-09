"""Scientific declarations and the callbacks a host may invoke.

Descriptions contain data only. Bindings stay in the scientific environment;
their callbacks receive scientific objects, never execution controllers.
"""

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum


def _identity(value, label):
    if not (
        type(value) is str and value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError(f"{label} must be a nonempty identity without whitespace or control characters")


def _records(values, record_type, label):
    if not (type(values) is tuple and all(type(value) is record_type for value in values)):
        raise TypeError(f"{label} must be an immutable tuple of {record_type.__name__} records")


def _unique_names(values, label):
    if len({value.name for value in values}) != len(values):
        raise ValueError(f"{label} cannot contain duplicate names")


def _callback(value, label):
    if not callable(value):
        raise TypeError(f"{label} must be callable")


class StateUse(Enum):
    BORROW = "borrow"
    CONSUME = "consume"


@dataclass(frozen=True, slots=True)
class StateParameter:
    name: str
    kind: str
    use: StateUse

    def __post_init__(self):
        _identity(self.name, "State parameter name")
        _identity(self.kind, "Scientific state kind")
        if type(self.use) is not StateUse:
            raise TypeError("State use must be a declared StateUse")


@dataclass(frozen=True, slots=True)
class StateOutput:
    """Bound the scientific state a successful call may return before dispatch."""

    name: str
    kind: str

    def __post_init__(self):
        _identity(self.name, "Output state name")
        _identity(self.kind, "Output scientific state kind")


@dataclass(frozen=True, slots=True)
class CallDescription:
    name: str
    input_format: str
    result_format: str
    state_inputs: tuple[StateParameter, ...]
    state_outputs: tuple[StateOutput, ...]
    resource_profile: str

    def __post_init__(self):
        _identity(self.name, "Call name")
        _identity(self.input_format, "Input format")
        _identity(self.result_format, "Result format")
        _identity(self.resource_profile, "Call resource profile")
        _records(self.state_inputs, StateParameter, "State parameters")
        _unique_names(self.state_inputs, "State parameters")
        _records(self.state_outputs, StateOutput, "State outputs")
        _unique_names(self.state_outputs, "State outputs")


@dataclass(frozen=True, slots=True)
class ImplementationDescription:
    binding_id: str
    binding_revision: str
    contract_version: int
    calls: tuple[CallDescription, ...]
    instance_resource_profile: str

    def __post_init__(self):
        _identity(self.binding_id, "Binding identity")
        _identity(self.binding_revision, "Binding revision")
        _identity(self.instance_resource_profile, "Instance resource profile")
        if not (type(self.contract_version) is int and 1 <= self.contract_version):
            raise ValueError("Contract version must be a positive integer")
        _records(self.calls, CallDescription, "Call descriptions")
        if not self.calls:
            raise ValueError("An implementation must declare at least one call")
        _unique_names(self.calls, "Call descriptions")


@dataclass(frozen=True, slots=True)
class CallRef:
    binding_id: str
    binding_revision: str
    name: str

    def __post_init__(self):
        _identity(self.binding_id, "Binding identity")
        _identity(self.binding_revision, "Binding revision")
        _identity(self.name, "Call name")


@dataclass(frozen=True, slots=True)
class InstanceHooks:
    """Construct bookkeeping first; the host retains it before initialization."""

    construct: Callable[[], object] = field(repr=False)
    initialize: Callable[[object], None] = field(repr=False)
    dispose: Callable[[object], None] = field(repr=False)

    def __post_init__(self):
        _callback(self.construct, "Instance constructor")
        _callback(self.initialize, "Instance initializer")
        _callback(self.dispose, "Instance disposal")


@dataclass(frozen=True, slots=True)
class AnalysisHooks:
    """Own scientific analysis state even when preparation has not succeeded."""

    construct: Callable[[object | None], object] = field(repr=False)
    initialize: Callable[[object], None] = field(repr=False)
    dispose: Callable[[object], None] = field(repr=False)

    def __post_init__(self):
        _callback(self.construct, "Analysis constructor")
        _callback(self.initialize, "Analysis initializer")
        _callback(self.dispose, "Analysis disposal")


@dataclass(frozen=True, slots=True)
class ReturnedState:
    """New scientific state whose value and disposal remain in this host."""

    name: str
    kind: str
    value: object = field(repr=False)
    dispose: Callable[[object], None] = field(repr=False)

    def __post_init__(self):
        _identity(self.name, "Returned state name")
        _identity(self.kind, "Returned scientific state kind")
        _callback(self.dispose, "Returned state disposal")


@dataclass(frozen=True, slots=True)
class ScientificReturn:
    """Retain this return and its state before encoding value can fail.

    A missing value disposer declares self-contained Python output. State values
    have their own disposal hooks and must not share that disposal responsibility.
    """

    value: object = field(repr=False)
    states: tuple[ReturnedState, ...]
    dispose_value: Callable[[object], None] | None = field(repr=False)

    def __post_init__(self):
        _records(self.states, ReturnedState, "Returned scientific states")
        _unique_names(self.states, "Returned scientific states")
        if self.dispose_value is not None:
            _callback(self.dispose_value, "Scientific return disposal")


@dataclass(frozen=True, slots=True)
class CallBinding:
    """Adapt ordinary scientific functions to one explicitly registered call."""

    description: CallDescription
    decode_input: Callable[[object | None, object | None, bytes], object] = field(repr=False)
    invoke: Callable[[object | None, object | None, object, Mapping[str, object]], ScientificReturn] = field(repr=False)
    encode_result: Callable[[object], bytes] = field(repr=False)

    def __post_init__(self):
        if type(self.description) is not CallDescription:
            raise TypeError("A call binding requires its CallDescription")
        _callback(self.decode_input, "Scientific input decoder")
        _callback(self.invoke, "Scientific invocation")
        _callback(self.encode_result, "Scientific result encoder")


@dataclass(frozen=True, slots=True)
class ImplementationBinding:
    """Configured scientific code; scheduler-facing metadata is description."""

    description: ImplementationDescription
    instance: InstanceHooks | None
    analysis: AnalysisHooks | None
    calls: tuple[CallBinding, ...]

    def __post_init__(self):
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


class ScientificRejection(Exception):
    """A reviewed scientific input rejection; message and cause are diagnostics."""

    def __init__(self, code: str, message: str):
        _identity(code, "Scientific rejection code")
        if not (type(message) is str and message):
            raise ValueError("Scientific rejection requires an explanatory message")
        self.code, self.message = code, message
        super().__init__(message)
