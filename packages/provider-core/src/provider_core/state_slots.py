"""Typed scientific state ports over the execution host's portable references.

These helpers connect provider-owned Python types at assembly and projection.
They grant no dispatch, consumption or retry authority: the execution backend
still checks live reference membership, exact provenance and actual state use.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from provider_core.contribution import (
    CallBinding,
    ReturnedState,
    StateOutput,
    StateParameter,
    StateUse,
    _identity,
    ref,
)

from provider_core.hosting import Operation, OperationRef, StateRef
from provider_core.type_requirements import require_instance, require_type


@dataclass(frozen=True, slots=True)
class StateType[T]:
    """A scientific state type; its qualified Python name supplies the identity.

    Explicit names remain available for existing portable contracts. Declare
    inferred input/output ports as class attributes so Python supplies their
    names before the contribution is adopted.
    """

    name: str = field(default=None, kw_only=True)
    value_type: type[T]

    def __post_init__(self) -> None:
        require_instance(self.value_type, type)

        if self.name is None:
            object.__setattr__(self, "name", self.value_type.__module__ + "." + self.value_type.__qualname__)

        _identity(self.name, "Scientific state kind")

    def output(self, *, name: str | None = None, dispose: Callable[[T], None]) -> StateOutputSlot[T]:
        return StateOutputSlot(name=name, kind=self, dispose=dispose)

    def input(self, *, name: str | None = None, use: StateUse) -> StateInputSlot[T]:
        return StateInputSlot(name=name, kind=self, use=use)


@dataclass(frozen=True, slots=True)
class StateHandle[T]:
    """A typed view of an observed reference, not a live scientific object."""

    kind: StateType[T]
    reference: StateRef

    def __post_init__(self) -> None:
        if self.reference.kind != self.kind.name:
            raise ValueError("State reference kind differs from its typed handle")

    @property
    def producer(self) -> OperationRef:
        return self.reference.producer


@dataclass(frozen=True, slots=True, kw_only=True)
class StateOutputSlot[T]:
    """Own a producer's output identity and its scientific disposal callback."""

    name: str | None = None
    kind: StateType[T]
    dispose: Callable[[T], None]
    _inferred: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_inferred", self.name is None)

        if self.name is not None:
            StateOutput(name=self.name, kind=self.kind.name)

        require_instance(self.kind.value_type, type)

        if not callable(self.dispose):
            raise TypeError(f"{self.dispose=} should be callable")

    def __set_name__(self, owner: type, name: str) -> None:
        """Bind an omitted port name to its unambiguous Python attribute."""

        if (self._inferred) and (self.name is not None) and (self.name != name):
            raise ValueError("An inferred state port cannot have different Python attribute names")

        if self.name is None:
            object.__setattr__(self, "name", name)

    @property
    def value_type(self) -> type[T]:
        return self.kind.value_type

    @property
    def description(self) -> StateOutput:
        return StateOutput(name=self.name, kind=self.kind.name)

    def returned(self, value: T) -> ReturnedState[T]:
        """Give the host custody before any later state validation or encoding."""

        return ReturnedState(name=self.name, kind=self.kind.name, value=value, dispose=self.dispose)

    async def reference(self, call: CallBinding[Any, Any, Any], operation: Operation) -> StateHandle[T]:
        """Project this declared output by observing the retained producer owner.

        This performs one zero-wait observation. Callers that require completed
        science await completion first; an unpublished output is an error here.
        Observation identities are correlation evidence, not authentication.
        A fabricated record cannot replace the backend's live StateRef checks.
        """
        target = ref(call)

        if not any((slot is self) for slot in call._output_slots):
            raise ValueError(f"One of {call._output_slots=} should be {self=}")

        observation = await operation.observe(after=None, wait_seconds=0)

        if observation.operation != operation.reference:
            raise ValueError("Observation belongs to another producer operation")

        if observation.target != target:
            raise ValueError("Observation belongs to another producer call or configured revision")

        for failure in observation.failures:
            if failure.phase == "state_adoption":
                raise failure.cause

        for output in observation.states:
            if output.name == self.name:
                reference = output.reference

                if (
                    (reference.host != observation.operation.host) or
                    (reference.analysis != observation.operation.analysis) or
                    (reference.binding_id != target.binding_id) or
                    (reference.binding_revision != target.binding_revision) or
                    (reference.kind != self.kind.name) or
                    (reference.producer != operation.reference)
                ):
                    raise ValueError("State output provenance differs from its producer observation")

                return StateHandle(kind=self.kind, reference=reference)

        raise ValueError(f"Producer observation contains no output {self.name!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class StateInputSlot[T]:
    """Name a consumer input independently of the producer's output name."""

    name: str | None = None
    kind: StateType[T]
    use: StateUse
    _inferred: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_inferred", self.name is None)

        if self.name is not None:
            StateParameter(name=self.name, kind=self.kind.name, use=self.use)

    def __set_name__(self, owner: type, name: str) -> None:
        """Bind an omitted port name to its unambiguous Python attribute."""

        if (self._inferred) and (self.name is not None) and (self.name != name):
            raise ValueError("An inferred state port cannot have different Python attribute names")

        if self.name is None:
            object.__setattr__(self, "name", name)

    @property
    def value_type(self) -> type[T]:
        return self.kind.value_type

    @property
    def description(self) -> StateParameter:
        return StateParameter(name=self.name, kind=self.kind.name, use=self.use)

    def read(self, values: Mapping[str, object]) -> T:
        """Read a host-admitted live value while preserving its concrete type."""
        value = values[self.name]

        require_type(value, self.kind.value_type)

        return cast(T, value)


def state_references(*, states: Mapping[StateInputSlot, StateHandle]) -> dict[str, StateRef]:
    """Project typed provider ports and handles into portable invocation references."""

    require_instance(states, Mapping)

    references = {}

    for (state_port, state_handle) in states.items():
        require_type(state_port, StateInputSlot)

        require_type(state_handle, StateHandle)

        if (state_handle.kind != state_port.kind) or (state_handle.reference.kind != state_port.kind.name):
            raise ValueError("State handle has a different scientific kind or Python type")  # todo

        if state_port.name in references:
            raise ValueError(f"Some state ports in {...} have the same {state_port.name=}")  # todo

        references[state_port.name] = state_handle.reference

    return references


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotStateBinding[T]:
    """Assemble one provider state struct from explicit typed input slots."""

    value_type: type[T]
    inputs: tuple[StateInputSlot[Any], ...]
    assemble: Callable[[Mapping[str, object]], T]

    def __post_init__(self) -> None:
        require_instance(self.value_type, type)

        if not callable(self.assemble):
            raise TypeError(f"{self.assemble=} should be callable")

        require_type(self.inputs, tuple)

        for state_input_slot in self.inputs:
            require_type(state_input_slot, StateInputSlot)

        if len(set(self.names)) != len(self.inputs):
            raise ValueError("Consumer input slot names must be unique")

    @property
    def names(self) -> tuple[str, ...]:
        names = []

        for slot in self.inputs:
            names.append(slot.name)

        return tuple(names)

    @property
    def parameters(self) -> tuple[StateParameter, ...]:
        parameters = []

        for slot in self.inputs:
            parameters.append(slot.description)

        return tuple(parameters)

    @property
    def input_types(self) -> Mapping[str, type[object]]:
        return {slot.name: slot.value_type for slot in self.inputs}

    def bind(self, values: Mapping[str, object]) -> T:
        if set(values) != set(self.names):
            raise ValueError("Live state values differ from the declared consumer inputs")

        for slot in self.inputs:
            slot.read(values)

        state = self.assemble(values)

        require_type(state, self.value_type)

        return state
