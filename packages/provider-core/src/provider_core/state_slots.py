"""Typed scientific state ports over the execution host's portable references.

These helpers connect provider-owned Python types at assembly and projection.
They grant no dispatch, consumption or retry authority: the execution backend
still checks live reference membership, exact provenance and actual state use.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from provider_core.contribution import (
    CallBinding, ReturnedState, StateOutput, StateParameter, StateUse, _identity, ref,
)
from provider_core.hosting import Operation, OperationRef, StateArgument, StateRef


@dataclass(frozen=True, slots=True)
class StateKind[T]:
    """One declared scientific kind and its actual provider-owned Python type."""

    name: str
    value_type: type[T]

    def __post_init__(self) -> None:
        _identity(self.name, "Scientific state kind")
        if not isinstance(self.value_type, type):
            raise TypeError("A scientific state kind must name a concrete Python type")

    def output(self, *, name: str, dispose: Callable[[T], None]) -> StateOutputSlot[T]:
        return StateOutputSlot(name=name, kind=self, dispose=dispose)

    def input(self, *, name: str, use: StateUse) -> StateInputSlot[T]:
        return StateInputSlot(name=name, kind=self, use=use)


@dataclass(frozen=True, slots=True)
class StateHandle[T]:
    """A typed view of an observed reference, not a live scientific object."""

    kind: StateKind[T]
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

    name: str
    kind: StateKind[T]
    dispose: Callable[[T], None]

    def __post_init__(self) -> None:
        StateOutput(name=self.name, kind=self.kind.name)
        if not isinstance(self.kind.value_type, type) or not callable(self.dispose):
            raise TypeError("An output slot needs a concrete scientific type and disposer")

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
        if not any(slot is self for slot in call._output_slots):
            raise ValueError("Output slot is not declared by this exact call")
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
                    reference.host != observation.operation.host
                    or reference.analysis != observation.operation.analysis
                    or reference.binding_id != target.binding_id
                    or reference.binding_revision != target.binding_revision
                    or reference.kind != self.kind.name
                    or reference.producer != operation.reference
                ):
                    raise ValueError("State output provenance differs from its producer observation")
                return StateHandle(kind=self.kind, reference=reference)
        raise ValueError(f"Producer observation contains no output {self.name!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class StateInputSlot[T]:
    """Name a consumer input independently of the producer's output name."""

    name: str
    kind: StateKind[T]
    use: StateUse

    def __post_init__(self) -> None:
        StateParameter(name=self.name, kind=self.kind.name, use=self.use)

    @property
    def value_type(self) -> type[T]:
        return self.kind.value_type

    @property
    def description(self) -> StateParameter:
        return StateParameter(name=self.name, kind=self.kind.name, use=self.use)

    def bind(self, handle: StateHandle[T]) -> StateArgument:
        """Describe a connection; actual guarded dispatch decides state use."""
        if handle.kind.name != self.kind.name or handle.reference.kind != self.kind.name or handle.kind.value_type is not self.kind.value_type:
            raise ValueError("State handle has a different scientific kind or Python type")
        return StateArgument(name=self.name, reference=handle.reference)

    def read(self, values: Mapping[str, object]) -> T:
        """Read a host-admitted live value while preserving its concrete type."""
        value = values[self.name]
        if type(value) is not self.kind.value_type:
            raise TypeError(f"State input {self.name!r} requires {self.kind.value_type.__qualname__}")
        return cast(T, value)


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotStateBinding[T]:
    """Assemble one provider state struct from explicit typed input slots."""

    value_type: type[T]
    inputs: tuple[StateInputSlot[Any], ...]
    assemble: Callable[[Mapping[str, object]], T]

    def __post_init__(self) -> None:
        if not isinstance(self.value_type, type) or not callable(self.assemble):
            raise TypeError("A scientific state binding needs a concrete type and assembler")
        if type(self.inputs) is not tuple or any(type(slot) is not StateInputSlot for slot in self.inputs):
            raise TypeError("State inputs must be an immutable tuple of StateInputSlot objects")
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
        if type(state) is not self.value_type:
            raise TypeError(f"Scientific state assembler must return {self.value_type.__qualname__}")
        return state
