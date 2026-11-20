"""The NumPy array operations exercised by the dummy provider.

Storage is independent mutable Python data. This mock implements rectangular
array creation, scalar indexing, copies, finite masks and floating reductions;
it does not emulate broadcasting, views or fixed-width integer overflow.
"""

from __future__ import annotations

import builtins
import math
from dataclasses import dataclass

from ._type_requirements import require_instance, require_type


@dataclass(frozen=True, slots=True, kw_only=True)
class _DType:
    kind: str
    scalar_type: type


def _unpack(array_input: object) -> tuple[tuple[int, ...], list]:
    if isinstance(array_input, (list, tuple)):
        if not array_input:
            return ((0,), [])

        children = [_unpack(array_input=child) for child in array_input]
        child_shape = children[0][0]

        if not all((shape == child_shape) for (shape, _) in children):
            child_shapes = [shape for (shape, _) in children]

            raise ValueError(f"Array input must be rectangular; {child_shapes=}")

        return ((len(children), *child_shape), [scalar for (_, scalars) in children for scalar in scalars])

    require_type(array_input, (bool, int, float, complex, str))

    return ((), [array_input])


def _inferred_type(*, scalars: list) -> type:
    scalar_types = {type(scalar) for scalar in scalars}

    for promoted_type in (str, complex, float, int, bool):
        if promoted_type in scalar_types:
            return promoted_type

    return float


def _nested_list(*, scalars: list, shape: tuple[int, ...]):
    if not shape:
        return scalars[0]

    block_size = math.prod(shape[1:])

    return [
        _nested_list(scalars=scalars[index * block_size:(index + 1) * block_size], shape=shape[1:])
        for index in range(shape[0])
    ]


class ndarray:
    """Owned array storage, constructed through array() or asarray().

    The provider uses dimension and dtype metadata, scalar indexing, copies and
    list conversion. Slice indexing and multidimensional indexing are outside
    this mock's contract and fail rather than returning an incorrect result.
    """

    __slots__ = ("_scalars", "_shape", "_dtype")

    def __init__(self, *, _scalars: list, shape: tuple[int, ...], dtype: _DType) -> None:
        self._scalars = _scalars
        self._shape = shape
        self._dtype = dtype

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def dtype(self) -> _DType:
        return self._dtype

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return len(self._scalars)

    def copy(self) -> ndarray:
        return ndarray(_scalars=self._scalars.copy(), shape=self.shape, dtype=self.dtype)

    def tolist(self):
        return _nested_list(scalars=self._scalars, shape=self.shape)

    def all(self) -> bool:
        return builtins.all(self._scalars)

    def __bool__(self) -> bool:
        if not (self.size == 1):
            raise ValueError(f"Array truth is ambiguous; use an explicit reduction; {self.shape=}")

        return bool(self._scalars[0])

    def __eq__(self, other):
        raise NotImplementedError("Elementwise array comparisons are outside this mock's contract")

    def __getitem__(self, index: int):
        require_type(index, int)

        if not (self.ndim == 1):
            raise NotImplementedError(f"Only vector scalar indexing is implemented; {self.shape=}")

        return self._scalars[index]

    def __setitem__(self, index: int, scalar_element: object) -> None:
        require_type(index, int)

        if not (self.ndim == 1):
            raise NotImplementedError(f"Only vector scalar assignment is implemented; {self.shape=}")

        self._scalars[index] = self.dtype.scalar_type(scalar_element)


def array(object: object, *, dtype: type | None = None) -> ndarray:
    """Create independent array storage; support inferred or explicit float dtype."""

    if isinstance(object, ndarray):
        shape = object.shape
        scalars = object._scalars
        scalar_type = object.dtype.scalar_type
    else:
        (shape, scalars) = _unpack(array_input=object)
        scalar_type = _inferred_type(scalars=scalars)

    if dtype is not None:
        if not (dtype is float):
            raise NotImplementedError(f"Only explicit float dtype is implemented; {dtype=}")

        scalar_type = float

    kinds = {bool: "b", int: "i", float: "f", complex: "c", str: "U"}

    return ndarray(
        _scalars=[scalar_type(scalar) for scalar in scalars],
        shape=shape,
        dtype=_DType(kind=kinds[scalar_type], scalar_type=scalar_type),
    )


def asarray(a: object, *, dtype: type) -> ndarray:
    """Reuse matching array storage or convert it to the requested float dtype."""

    if not (dtype is float):
        raise NotImplementedError(f"Only explicit float dtype is implemented; {dtype=}")

    if isinstance(a, ndarray) and (a.dtype.scalar_type is dtype):
        return a

    return array(object=a, dtype=dtype)


def isfinite(numeric_input: object, /) -> bool | ndarray:
    """Return scalar finiteness or a shape-preserving boolean array mask."""

    if isinstance(numeric_input, ndarray):
        return ndarray(
            _scalars=[isfinite(scalar) for scalar in numeric_input._scalars],
            shape=numeric_input.shape,
            dtype=_DType(kind="b", scalar_type=bool),
        )

    if isinstance(numeric_input, complex):
        return math.isfinite(numeric_input.real) and math.isfinite(numeric_input.imag)

    return math.isfinite(numeric_input)


def dot(a: ndarray, b: ndarray) -> float:
    """Compute a floating inner product; mismatched shapes always fail."""

    require_instance(a, ndarray)

    require_instance(b, ndarray)

    if not (a.ndim == b.ndim == 1):
        raise NotImplementedError(f"Only vector dot products are implemented; {a.shape=}; {b.shape=}")

    if not (a.shape == b.shape):
        raise ValueError(f"Dot product shapes must match; {a.shape=}; {b.shape=}")

    if not ((a.dtype.kind in "iuf") and (b.dtype.kind in "iuf")):
        raise NotImplementedError(f"Only real numeric dot products are implemented; {a.dtype=}; {b.dtype=}")

    return builtins.sum(((float(left) * float(right)) for (left, right) in zip(a._scalars, b._scalars, strict=True)), start=0.0)


def sum(a: ndarray, *, dtype: type) -> float:
    """Reduce real array elements using the example's explicit float accumulator."""

    require_instance(a, ndarray)

    if not ((dtype is float) and (a.dtype.kind in "iuf")):
        raise NotImplementedError(f"Only real arrays with float accumulation are implemented; {dtype=}; {a.dtype=}")

    return builtins.sum((float(scalar) for scalar in a._scalars), start=0.0)
