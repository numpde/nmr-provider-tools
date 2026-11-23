"""Author scientific components as configured objects with exported methods.

The host adopts the supplied scientific object and derives its execution
contract. Initialization starts only after ownership transfers. Each analysis
retains its own optional session before initialization begins.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import hashlib
import inspect
import json
import pathlib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field, replace
from threading import Lock
from types import FunctionType, NoneType
from typing import ClassVar, Protocol, get_args, get_origin, get_type_hints

from provider_core.contribution import (
    CallBinding,
    CallDescription,
    CallRef,
    ConcurrencyRequirement,
    ImplementationDescription,
    InterruptionSupport,
    IsolationRequirement,
    NoInput,
    ProgressEvent as ProgressEvent,
    ResourceRequirement,
    ScientificControl,
    ScientificReturn,
    StateUse as StateUse,
    _MethodCall,
    _ScientificCallShape,
    _require_state_kinds,
    _require_resource_requirements,
    description_document,
    ref,
)

from provider_core.payload import JsonDataclassCodec, PayloadCodec
from provider_core.hosting import AllocationRef
from provider_core.state_slots import StateInputSlot, StateOutputSlot
from provider_core.state_slots import StateType as StateType
from provider_core.type_requirements import require_instance, require_subclass, require_type


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetRef:
    """Content identity; deployment supplies and retains the corresponding file."""

    digest: str
    size_bytes: int
    media_type: str


class AssetAccess(Protocol):
    """Resolve already verified deployment snapshots for one allocation.

    The deployment retains the snapshot while dependent science is alive.
    A returned path is a lookup result; filesystem isolation is deployment-owned.
    """

    def path(self, *, asset: AssetRef) -> pathlib.Path: ...


class NoAssets:
    """An allocation with no file assets cannot resolve a content reference."""

    def path(self, *, asset: AssetRef) -> pathlib.Path:
        raise LookupError(f"{asset.digest=} is not granted to this allocation")


class _Custody:
    """Serialize handover across hosts; retirement never makes an object fresh."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.owner: AllocationRef | None = None


class Contribution:
    """Base class for a scientific object that the SDK runs and cleans up.

    Provider startup code creates an SDK execution manager, such as InProcessExecutionHost,
    and gives it this object. That manager is called the host below. It retains
    the object, initializes it before scientific work, runs admitted operations,
    and calls its cleanup method when provider shutdown requests retirement.

    Write an ordinary subclass with your own constructor arguments and fields.
    If you override __init__, call super().__init__() to establish ownership
    bookkeeping. The SDK does not read, serialize, freeze or hash your settings.
    This base class supplies that bookkeeping, empty initialize/close methods,
    and no separate analysis session by default.

    Construct the object without acquiring external resources, then pass it to
    host.adopt(contribution=object, requirements=...). The host
    validates execution support before accepting ownership. Requirements describe
    what science needs; adoption does not discover or reserve hardware. Rejection leaves the
    object with you; the host neither initializes nor closes it. Success retains
    that exact object. It cannot be adopted again by this or another host, even
    after retirement. Construct a new object for another allocation.

    Acquire model, file or device resources in this optional override:

        def initialize(self, *, assets: AssetAccess) -> None:
            ...

    The embedded host calls initialize lazily on the allocation's executor before
    scientific work, and reuses the initialized object across analyses. assets
    resolves deployment-provided verified files; an allocation without assets
    refuses lookup. You may instead retain your deployment's checkpoint locator
    in the constructor and verify its contents when loading.

    Retain each partial resource owner on self before a loading step can raise.
    Release those resources in the optional synchronous close(self) -> None
    override. It must also handle an object that was never initialized. Retirement
    waits for dependent work before calling close; a failed initialization still
    uses this same object's close. Failed cleanup retains the object and original
    error rather than retrying the disposer automatically. The caller must request
    and observe host retirement; abandoning a Python reference does not do that.

    Mark each callable scientific entrypoint with @operation(). Exported methods
    are synchronous instance methods with keyword-only arguments and concrete
    input/result annotations. Unmarked helpers are not exported to the host.
    Direct Python calls stay ordinary calls: hosting does not
    wrap them or enforce its lifecycle, permissions or resource limits.

    For managed local execution, submit a bound method such as object.predict to
    analysis.submit_method, with that object's reservation and execution limits.
    The SDK assigns operation identities; retain the returned operation handle
    to observe it again. Submitting the method again requests new work.
    A class member such as MyContribution.predict does not identify a receiver
    and is rejected at local submission. Operation declarations describe optional
    state ports, progress, interruption and resource requirements; see operation
    for their arguments. The provider chooses scientific sequencing.

    If science needs a separate session for each analysis, set analysis_type to
    an Analysis[YourContribution] subclass. The SDK constructs and retains that
    session before calling its initialize, and passes it to exported methods
    through an argument annotated with that session type. Implement its initialize
    and close rather than overriding its SDK-owned constructor. Leave
    analysis_type as None when no separate session is needed. Session cleanup,
    produced-state disposal, result release and contribution retirement have
    separate owners; finishing one does not imply the others have finished.
    """

    analysis_type: ClassVar[type[Analysis] | None] = None

    def __init__(self) -> None:
        self._custody = _Custody()

    def __setattr__(self, name: str, value: object) -> None:
        if (name == "_custody") and hasattr(self, name):
            raise AttributeError(f"Cannot replace {name=}; custody is fixed for this object's lifetime")

        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name == "_custody":
            raise AttributeError(f"Cannot delete {name=}; custody is fixed for this object's lifetime")

        object.__delattr__(self, name)

    def initialize(self, *, assets: AssetAccess) -> None:
        """Initialize owned resources; stateless contributions need no override."""

    def close(self) -> None:
        """Release owned resources after the host has settled dependent work."""


class Analysis[ContributionT]:
    """Optional scientific session owned by one analysis, before its first call.

    Session construction is SDK-owned. initialize may acquire resources only
    after the host retains this owner; close must handle partial initialization.
    """

    def __init__(self, *, contribution: ContributionT) -> None:
        self._contribution = contribution

    @property
    def contribution(self) -> ContributionT:
        return self._contribution

    def initialize(self) -> None:
        """Initialize analysis-local resources after custody is established."""

    def close(self) -> None:
        """Dispose analysis-local resources independently of retained results."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Declaration:
    state_inputs: tuple[StateInputSlot, ...]
    state_outputs: tuple[StateOutputSlot, ...]
    resources: frozenset[ResourceRequirement] | None
    concurrency: ConcurrencyRequirement
    interruption: InterruptionSupport
    progress: bool
    input_codec: PayloadCodec | None
    result_codec: PayloadCodec | None


# silent-defaults: allow-contract -- Unspecified capabilities declare no state, progress or interruption; omitted codecs derive from annotations and resources come from adoption.
def operation(
    *,
    state_inputs: tuple[StateInputSlot, ...] = (),
    state_outputs: tuple[StateOutputSlot, ...] = (),
    requirements: frozenset[ResourceRequirement] | None = None,
    concurrency: ConcurrencyRequirement = ConcurrencyRequirement.INSTANCE_SERIAL,
    interruption: InterruptionSupport = InterruptionSupport.NONE,
    progress: bool = False,
    input_codec: PayloadCodec | None = None,
    result_codec: PayloadCodec | None = None,
) -> Callable:
    """Export an ordinary method without wrapping or changing direct calls.

    Parameters after self are keyword-only. Annotations identify the payload,
    optional analysis session and control; explicit state ports name live inputs.
    Omitted resources use the contribution's common call requirements. Explicit
    empty resources mean that this operation has no call resource requirements.
    """

    declaration = _Declaration(
        state_inputs=state_inputs,
        state_outputs=state_outputs,
        resources=requirements,
        concurrency=concurrency,
        interruption=interruption,
        progress=progress,
        input_codec=input_codec,
        result_codec=result_codec,
    )

    def decorate(function: Callable) -> Callable:
        # Python supplies the decorated function positionally; the method itself
        # remains unchanged, including its keyword-only scientific signature.
        require_instance(function, FunctionType)

        if "__scientific_operation__" in function.__dict__:
            raise ValueError(f"{function.__qualname__=} should have one operation declaration")

        function.__scientific_operation__ = declaration

        return function

    return decorate


@dataclass(frozen=True, slots=True, kw_only=True)
class _NamedStates:
    """Pass admitted live inputs as their declared method keyword arguments."""

    inputs: tuple[StateInputSlot, ...]
    value_type: type = dict

    def __post_init__(self) -> None:
        require_type(self.inputs, tuple)

        for state_input_slot in self.inputs:
            require_type(state_input_slot, StateInputSlot)

    @property
    def names(self) -> tuple[str, ...]:
        # repo-check: allow-tuple-generator -- The state-binding protocol exposes immutable input names for registration and admission.
        return tuple(slot.name for slot in self.inputs)

    @property
    def parameters(self) -> tuple:
        # repo-check: allow-tuple-generator -- These immutable state parameters enter the registered call description.
        return tuple(slot.description for slot in self.inputs)

    @property
    def input_types(self) -> Mapping[str, type]:
        return {slot.name: slot.value_type for slot in self.inputs}

    def bind(self, values: Mapping[str, object]) -> dict[str, object]:
        if (state_argument_names := set(values)) != (declared_state_names := set(self.names)):
            raise ValueError(f"{state_argument_names=} should match {declared_state_names=}")

        for slot in self.inputs:
            slot.read(values=values)

        return dict(values)


def _members(*, contribution: type[Contribution]) -> tuple[tuple[str, Callable], ...]:
    effective = {}

    for parent in reversed(contribution.__mro__):
        effective.update(vars(parent))

    exported_methods = []

    for (member_name, member) in sorted(effective.items()):
        if isinstance(member, (staticmethod, classmethod)):
            if "__scientific_operation__" in member.__func__.__dict__:
                raise TypeError(f"{member_name=} should be an instance method")

        if isinstance(member, property):
            if (member.fget is not None) and ("__scientific_operation__" in member.fget.__dict__):
                raise TypeError(f"{member_name=} should not be a property")

        if inspect.isfunction(object=member) and ("__scientific_operation__" in member.__dict__):
            if member_name in ("__init__", "initialize", "close"):
                raise ValueError(f"Lifecycle {member_name=} should not also be an operation")

            exported_methods.append((member_name, member))

    if not (exported_methods):
        raise ValueError(f"{contribution.__qualname__=} should export at least one operation")

    if len({function for (_, function) in exported_methods}) != len(exported_methods):
        function_counts = Counter(function for (_, function) in exported_methods)
        aliased_function = next(function for (function, count) in function_counts.items() if count > 1)
        aliased_method_names = [name for (name, function) in exported_methods if function is aliased_function]

        raise ValueError(f"{aliased_method_names=} should name distinct operations")

    return tuple(exported_methods)


def _codec_schema(codec: PayloadCodec) -> object:
    # Custom codecs own their representation through their declared format
    if isinstance(codec, JsonDataclassCodec):
        return codec.json_schema()

    return {"format": codec.format}


def _codec(*, value_type: type, supplied: PayloadCodec | None) -> PayloadCodec:
    if supplied is not None:
        require_instance(supplied, PayloadCodec)

        codec_value_type = supplied.value_type

        if not (codec_value_type is value_type):
            raise TypeError(f"{codec_value_type=} should be {value_type=}")

        return supplied

    return JsonDataclassCodec(
        format=f"python-json.v1:{value_type.__module__}.{value_type.__qualname__}",  # machine-string: Payload codec identity contains the wire format version and dotted value type
        value_type=value_type,
    )


def assert_synchronous(function: Callable) -> None:
    if not inspect.isfunction(object=function):
        raise TypeError(f"{function=} must be an ordinary instance method")

    if not (
        (not inspect.iscoroutinefunction(obj=function)) and
        (not inspect.isgeneratorfunction(obj=function)) and
        (not inspect.isasyncgenfunction(obj=function))
    ):
        raise TypeError(f"{function=} must be synchronous and cannot yield")


@dataclass(frozen=True, slots=True, kw_only=True)
class _LifecycleMethod:
    """Retain the exact validated lifecycle function of this contribution."""

    function: Callable
    receiver: str

    def invoke(self, *, instance: object, **arguments: object) -> None:
        self.function(**{self.receiver: instance, **arguments})


def _lifecycle(*, owner: type, method_name: str, expected_parameters: dict[str, type]) -> _LifecycleMethod:
    function = inspect.getattr_static(obj=owner, attr=method_name)

    assert_synchronous(function)
    parameters = tuple(inspect.signature(obj=function).parameters.values())

    if (not parameters) or (parameters[0].kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD):
        raise TypeError(f"{method_name=} should have an instance receiver")

    parameters_by_name = {parameter.name: parameter for parameter in parameters[1:]}

    if set(parameters_by_name) != set(expected_parameters):
        actual_parameter_names = sorted(parameters_by_name)
        expected_parameter_names = sorted(expected_parameters)

        raise TypeError(f"{method_name=}: {actual_parameter_names=} should match {expected_parameter_names=}")

    annotations = get_type_hints(obj=function)

    for (parameter_name, parameter) in parameters_by_name.items():
        if (parameter.kind is not inspect.Parameter.KEYWORD_ONLY) or (parameter.default is not inspect.Parameter.empty):
            raise TypeError(f"{method_name=}: {parameter_name=} should be required and keyword-only")

        if not (parameter_name in annotations):
            raise TypeError(f"{method_name=} requires an annotation for {parameter_name=}")

        if not (annotations[parameter_name] is expected_parameters[parameter_name]):
            raise TypeError(f"{method_name=}: {parameter_name=} should be annotated as {expected_parameters[parameter_name]=}")

    if not (annotations.get("return") is NoneType):
        raise TypeError(f"{method_name=} should declare a None return")

    return _LifecycleMethod(function=function, receiver=parameters[0].name)


def _compile_method(
    *,
    contribution: type[Contribution],
    method_name: str,
    function: Callable,
    resources: frozenset[ResourceRequirement],
) -> CallBinding:
    assert_synchronous(function)
    declaration = function.__scientific_operation__
    parameters = tuple(inspect.signature(obj=function).parameters.values())

    if (not parameters) or (parameters[0].kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD):
        raise TypeError(f"{method_name=} should have an ordinary instance receiver")

    if any((parameter.kind is not inspect.Parameter.KEYWORD_ONLY) for parameter in parameters[1:]):
        raise TypeError(f"{method_name=} should have keyword-only scientific arguments")

    if any((parameter.default is not inspect.Parameter.empty) for parameter in parameters):
        raise TypeError(f"{method_name=} should have required arguments without defaults")

    hints = get_type_hints(obj=function)
    state_binding = _NamedStates(inputs=declaration.state_inputs)

    if len(set(state_binding.names)) != len(state_binding.names):
        raise ValueError(f"{method_name=}: {state_binding.names=} should be unique")

    inputs = []
    analyses = []
    controls = []
    found_states = set()

    for parameter in parameters[1:]:
        if not (parameter.name in hints):
            raise TypeError(f"{method_name=} requires an annotation for {parameter.name=}")

        annotation = hints[parameter.name]

        if parameter.name in state_binding.input_types:
            if not (annotation is state_binding.input_types[parameter.name]):
                raise TypeError(f"{method_name=}: {parameter.name=} has {annotation=}; expected {state_binding.input_types[parameter.name]=}")

            found_states.add(parameter.name)
        elif annotation is ScientificControl:
            controls.append(parameter.name)
        elif (contribution.analysis_type is not None) and (annotation is contribution.analysis_type):
            analyses.append(parameter.name)
        else:
            inputs.append((parameter.name, annotation))

    if found_states != set(state_binding.names):
        actual_state_names = sorted(found_states)
        expected_state_names = sorted(state_binding.names)

        raise ValueError(f"{method_name=}: {actual_state_names=} should match {expected_state_names=}")

    if (len(inputs) > 1) or (len(analyses) > 1) or (len(controls) > 1):
        raise TypeError(f"{method_name=} should accept at most one payload, one analysis and one control argument")

    requires_control_argument = declaration.progress or (declaration.interruption is InterruptionSupport.COOPERATIVE)
    has_control_argument = bool(controls)

    if has_control_argument != requires_control_argument:
        raise ValueError(f"{method_name=}: {has_control_argument=} should match {requires_control_argument=}")

    if not ("return" in hints):
        raise TypeError(f"{method_name=} requires a return annotation")

    output = hints["return"]
    wrapped = get_origin(tp=output) is ScientificReturn

    if wrapped:
        (output,) = get_args(tp=output)

    if (declaration.state_outputs) and (not wrapped):
        raise TypeError(f"{method_name=} should return ScientificReturn[Result] when returning state")

    (input_name, input_type) = (None, NoInput)

    if inputs:
        (input_name, input_type) = inputs[0]

    analysis_type = contribution.analysis_type

    if analysis_type is None:
        analysis_type = NoneType

    analysis_parameter = None

    if analyses:
        analysis_parameter = analyses[0]

    control_parameter = None

    if controls:
        control_parameter = controls[0]

    input_codec = _codec(value_type=input_type, supplied=declaration.input_codec)
    result_codec = _codec(value_type=output, supplied=declaration.result_codec)

    callback = _MethodCall(
        function=function,
        receiver=parameters[0].name,
        shape=_ScientificCallShape(
            instance=contribution,
            analysis=analysis_type,
            input=input_type,
            state=state_binding.value_type,
            output=output,
        ),
        input_parameter=input_name,
        analysis_parameter=analysis_parameter,
        control_parameter=control_parameter,
        wrapped_result=wrapped,
    )

    output_descriptions = [slot.description for slot in declaration.state_outputs]

    return CallBinding(
        description=CallDescription(
            name=method_name,
            input_format=input_codec.format,
            result_format=result_codec.format,
            state_inputs=state_binding.parameters,
            state_outputs=tuple(output_descriptions),
            resources=resources if declaration.resources is None else declaration.resources,
            concurrency=declaration.concurrency,
            interruption=declaration.interruption,
            progress=declaration.progress,
        ),
        input_codec=input_codec,
        state_binding=state_binding,
        scientific_call=callback,
        result_codec=result_codec,
        _output_slots=declaration.state_outputs,
    )


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class HostedContribution:
    """SDK handle for a scientific contribution and its exported methods.

    A class-only handle compiles the same declarations without creating an
    object in the parent process. Exact-object adoption supplies contribution;
    remote construction supplies contribution_type and no live receiver.

    Equality and hashing use this handle's identity, not the scientific object's
    values. Provider-owned objects can be mutable, unhashable or define their own
    equality; none of those choices changes a hosted handle's set membership.

    The constructor-only host and allocation_name inputs pass through InitVar to
    __post_init__, which derives the allocation handle without duplicate fields.

    The revision identifies schemas and the exported
    execution contract. Deployment owns installed artifact provenance separately;
    this contract neither identifies code bytes nor proves method purity.
    """

    contribution: Contribution | None
    host: InitVar[str]
    allocation_name: InitVar[str]
    requirements: frozenset[ResourceRequirement]
    contribution_type: type[Contribution] | None = None
    constructor: Callable[[], Contribution] | None = field(default=None, repr=False, compare=False)
    resident_resources: frozenset[ResourceRequirement] = frozenset()
    isolation: IsolationRequirement = IsolationRequirement.SHARED_PROCESS
    description: ImplementationDescription = field(init=False)
    allocation: AllocationRef = field(init=False)
    _calls: tuple[CallBinding, ...] = field(init=False, repr=False, compare=False)
    _analysis_type: type[Analysis] | None = field(init=False, repr=False, compare=False)
    _initialize: _LifecycleMethod = field(init=False, repr=False, compare=False)
    _close: _LifecycleMethod = field(init=False, repr=False, compare=False)
    _analysis_initialize: _LifecycleMethod | None = field(init=False, repr=False, compare=False)
    _analysis_close: _LifecycleMethod | None = field(init=False, repr=False, compare=False)

    def __post_init__(self, host: str, allocation_name: str) -> None:
        if self.contribution is None:
            require_subclass(self.contribution_type, Contribution)

            contribution_type = self.contribution_type
        else:
            require_instance(self.contribution, Contribution)

            contribution_type = type(self.contribution)

            if self.constructor is not None:
                raise TypeError(f"{type(self.constructor)=}; exact-object adoption should not supply a constructor")

            if self.contribution_type is not None:
                require_subclass(self.contribution_type, Contribution)
                require_type(self.contribution, self.contribution_type)

        _require_resource_requirements(resource_requirements=self.requirements)
        object.__setattr__(self, "_initialize", _lifecycle(owner=contribution_type, method_name="initialize", expected_parameters={"assets": AssetAccess}))
        object.__setattr__(self, "_close", _lifecycle(owner=contribution_type, method_name="close", expected_parameters={}))
        analysis_type = contribution_type.analysis_type

        object.__setattr__(self, "_analysis_type", analysis_type)
        object.__setattr__(self, "_analysis_initialize", None)
        object.__setattr__(self, "_analysis_close", None)

        if analysis_type is not None:
            require_subclass(analysis_type, Analysis)

            analysis_constructor = analysis_type.__init__

            if not (analysis_constructor is Analysis.__init__):
                raise TypeError(f"{analysis_constructor=} should be {Analysis.__init__=}")

            analysis_allocator = analysis_type.__new__

            if not (analysis_allocator is object.__new__):
                raise TypeError(f"{analysis_allocator=} should be {object.__new__=}")

            object.__setattr__(self, "_analysis_initialize", _lifecycle(owner=analysis_type, method_name="initialize", expected_parameters={}))
            object.__setattr__(self, "_analysis_close", _lifecycle(owner=analysis_type, method_name="close", expected_parameters={}))

        members = _members(contribution=contribution_type)
        calls = []

        for (name, function) in members:
            call = _compile_method(
                contribution=contribution_type,
                method_name=name,
                function=function,
                resources=self.requirements,
            )

            object.__setattr__(call, "_owner", self)
            calls.append(call)

        object.__setattr__(self, "_calls", tuple(calls))
        _require_state_kinds(calls=self._calls)
        call_descriptions = [call.description for call in calls]

        description = ImplementationDescription(
            binding_id="allocation:" + host + ":" + allocation_name.encode(encoding="utf-8").hex(),
            binding_revision="configured-v4:sha256:" + "0" * 64,
            contract_version=1,
            configuration=None,
            assets=frozenset(),
            calls=frozenset(call_descriptions),
            resident_resources=self.resident_resources,
            isolation=self.isolation,
        )

        implementation_schema = description_document(description=description)

        del implementation_schema["binding_revision"]
        del implementation_schema["binding_id"]
        implementation_schema["analysis"] = None

        if self._analysis_type is not None:
            implementation_schema["analysis"] = f"{self._analysis_type.__module__}.{self._analysis_type.__qualname__}"  # machine-string: The execution contract identifies its analysis type by dotted Python import name

        implementation_schema["operation_schemas"] = [
            {
                "input": _codec_schema(call.input_codec),
                "result": _codec_schema(call.result_codec)
            }
            for call in calls
        ]

        encoded_contract = json.dumps(obj=implementation_schema, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode(encoding="ascii")
        revision = "configured-v4:sha256:" + hashlib.sha256(string=encoded_contract).hexdigest()

        object.__setattr__(self, "description", replace(description, binding_revision=revision))

        object.__setattr__(
            self,
            "allocation",
            AllocationRef(
                host=host,
                allocation=allocation_name,
                binding_id=self.description.binding_id,
                binding_revision=revision,
            )
        )

    def call(self, *, method: Callable) -> CallBinding:
        """Select an exported method of this receiver without string dispatch."""

        if inspect.ismethod(object=method):
            if self.contribution is None:
                raise ValueError(f"{method.__qualname__=} should be unbound while {self.contribution=}")

            if not (method.__self__ is self.contribution):
                raise ValueError(f"{id(method.__self__)=} should match {id(self.contribution)=}")

            method = method.__func__

        for call in self._calls:
            if call.scientific_call.function is method:
                return call

        raise ValueError("Method is not an exported operation of this hosted contribution")

    def reference(self, *, method: Callable) -> CallRef:
        return ref(call=self.call(method=method))

    def construct_instance(self) -> Contribution:
        """Construct on the host-owned worker after its lifetime is retained."""

        if self.contribution is not None:
            return self.contribution

        if self.constructor is None:
            raise RuntimeError(f"{self.contribution_type=} has no constructor; {self.allocation=}")

        instance = self.constructor()

        require_type(instance, self.contribution_type)

        custody = instance._custody

        with custody.lock:
            if custody.owner is not None:
                raise ValueError(f"Cannot claim {self.allocation=}; {custody.owner=} already owns the constructed contribution")

            custody.owner = self.allocation

        return instance

    def initialize_instance(self, *, instance: Contribution, assets: AssetAccess) -> None:
        self._initialize.invoke(instance=instance, assets=assets)

    def close_instance(self, *, instance: Contribution) -> None:
        self._close.invoke(instance=instance)

    def construct_analysis(self, *, instance: Contribution) -> Analysis | None:
        if self._analysis_type is None:
            return None

        analysis = object.__new__(self._analysis_type)

        Analysis.__init__(self=analysis, contribution=instance)

        return analysis

    def initialize_analysis(self, *, analysis: Analysis | None) -> None:
        if self._analysis_initialize is not None:
            self._analysis_initialize.invoke(instance=analysis)

    def close_analysis(self, *, analysis: Analysis | None) -> None:
        if self._analysis_close is not None:
            self._analysis_close.invoke(instance=analysis)
