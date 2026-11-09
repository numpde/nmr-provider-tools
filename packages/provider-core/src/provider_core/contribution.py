"""The durable boundary implemented by scientific contributions.

Descriptions are immutable data that orchestration can inspect without importing
scientific dependencies. Bindings stay inside the scientific host and contain the
provider-owned callables that interpret configuration, assets and scientific input.
"""

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
import re


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identity(value, label):
    if not (
        type(value) is str
        and value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError(f"{label} must be a nonempty identity without whitespace or control characters")


def _digest(value, label):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase sha256:<64 hex characters> digest")


def _records(values, record_type, label):
    if not (type(values) is tuple and all(type(value) is record_type for value in values)):
        raise TypeError(f"{label} must be an immutable tuple of {record_type.__name__} records")


def _unique(values, key, label):
    identities = [key(value) for value in values]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} cannot contain duplicates")


def _callback(value, label):
    if not callable(value):
        raise TypeError(f"{label} must be callable")


class StateUse(Enum):
    BORROW = "borrow"
    CONSUME = "consume"


class IsolationRequirement(Enum):
    """The minimum boundary needed around one initialized implementation."""

    SHARED_PROCESS = "shared_process"
    DEDICATED_PROCESS = "dedicated_process"


class ConcurrencyRequirement(Enum):
    """Where scientific calls must be serialized.

    INSTANCE_SERIAL permits independent initialized instances to run together.
    PROCESS_SERIAL covers process-global state such as logging or framework globals.
    PARALLEL declares no scientific serialization requirement beyond state claims.
    """

    INSTANCE_SERIAL = "instance_serial"
    PROCESS_SERIAL = "process_serial"
    PARALLEL = "parallel"


class InterruptionSupport(Enum):
    """Whether the call cooperates with a narrow stop signal."""

    NONE = "none"
    COOPERATIVE = "cooperative"


@dataclass(frozen=True, slots=True)
class ConfigurationDescription:
    """Identify the configured scientific settings without exposing their values."""

    format: str
    digest: str

    def __post_init__(self):
        _identity(self.format, "Configuration format")
        _digest(self.digest, "Configuration digest")


@dataclass(frozen=True, slots=True)
class AssetDescription:
    """Identify one immutable model or scientific asset supplied by deployment."""

    name: str
    media_type: str
    digest: str
    size_bytes: int

    def __post_init__(self):
        _identity(self.name, "Asset name")
        _identity(self.media_type, "Asset media type")
        _digest(self.digest, "Asset digest")
        if not (type(self.size_bytes) is int and 0 < self.size_bytes):
            raise ValueError("Asset byte count must be a positive integer")


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    """A neutral quantity that deployment must grant to the scientific host.

    Kinds, selectors and units are plain identities. The scheduler may compare
    them but never receives framework device objects or permission to acquire them.
    """

    kind: str
    amount: int
    unit: str
    selector: str | None = None
    exclusive: bool = False

    def __post_init__(self):
        _identity(self.kind, "Resource kind")
        _identity(self.unit, "Resource unit")
        if not (type(self.amount) is int and 0 < self.amount):
            raise ValueError("Resource amount must be a positive integer")
        if self.selector is not None:
            _identity(self.selector, "Resource selector")
        if type(self.exclusive) is not bool:
            raise TypeError("Resource exclusivity must be boolean")

    @property
    def identity(self):
        return self.kind, self.unit, self.selector


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
class ProgressEvent:
    """A bounded, provider-reviewed progress fact with no control authority."""

    stage: str
    completed: int | None = None
    total: int | None = None

    def __post_init__(self):
        _identity(self.stage, "Progress stage")
        if (self.completed is None) != (self.total is None):
            raise ValueError("Progress completed and total must be supplied together")
        if self.completed is not None:
            if not (
                type(self.completed) is int
                and type(self.total) is int
                and 0 <= self.completed <= self.total
                and 0 < self.total
            ):
                raise ValueError("Progress counts must satisfy 0 <= completed <= total and total > 0")


class ScientificControl(Protocol):
    """The only optional operational capability visible to an adapter."""

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

    def __post_init__(self):
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

    def __post_init__(self):
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

    def __post_init__(self):
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")
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
    """Retain this return and its state before encoding value can fail."""

    value: object = field(repr=False)
    states: tuple[ReturnedState, ...]
    dispose_value: Callable[[object], None] | None = field(repr=False)

    def __post_init__(self):
        _records(self.states, ReturnedState, "Returned scientific states")
        _unique(self.states, lambda value: value.name, "Returned scientific states")
        if self.dispose_value is not None:
            _callback(self.dispose_value, "Scientific return disposal")


@dataclass(frozen=True, slots=True)
class CallBinding:
    """Adapt ordinary scientific functions to one explicitly registered call."""

    description: CallDescription
    decode_input: Callable[[object | None, object | None, bytes, ScientificControl], object] = field(repr=False)
    invoke: Callable[
        [object | None, object | None, object, Mapping[str, object], ScientificControl],
        ScientificReturn,
    ] = field(repr=False)
    encode_result: Callable[[object], bytes] = field(repr=False)

    def __post_init__(self):
        if type(self.description) is not CallDescription:
            raise TypeError("A call binding requires its CallDescription")
        _callback(self.decode_input, "Scientific input decoder")
        _callback(self.invoke, "Scientific invocation")
        _callback(self.encode_result, "Scientific result encoder")


@dataclass(frozen=True, slots=True)
class ImplementationBinding:
    """Configured scientific code loaded only inside an authorized host."""

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
        self.code = code
        self.message = message
        super().__init__(message)


def description_document(description: ImplementationDescription) -> dict:
    """Return the scheduler-facing, JSON-compatible view of one binding."""
    if type(description) is not ImplementationDescription:
        raise TypeError("Description export requires an ImplementationDescription")

    def resource_document(requirement):
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
