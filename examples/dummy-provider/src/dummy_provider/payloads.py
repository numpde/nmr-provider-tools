"""Translate JSON sample sequences into the provider's numerical arrays.

The SDK's JSON codec owns structural admission, duplicate-field rejection and
finite-number encoding. This adapter owns the conversion to scientific arrays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import mock_numpy as np

from provider_core.payload import JsonDataclassCodec

from . import science


@dataclass(frozen=True, slots=True, kw_only=True)
class _JsonSamples:
    """Transport-only sequence; scientific input records retain arrays."""

    samples: tuple[float, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SamplesCodec[SampleInputT: science.PreparationInput | science.SumInput]:
    value_type: type[SampleInputT]
    _json_codec: JsonDataclassCodec[_JsonSamples] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_json_codec",
            JsonDataclassCodec(
                format=f"python-json.v1:{self.value_type.__module__}.{self.value_type.__qualname__}",  # machine-string: codec format identifiers contain the qualified Python type name, without expression labels.
                value_type=_JsonSamples,
            ),
        )

    @property
    def format(self) -> str:
        return self._json_codec.format

    def decode(self, payload: bytes) -> SampleInputT:
        document = self._json_codec.decode(payload=payload)

        return self.value_type(samples=np.asarray(a=document.samples, dtype=float))

    def encode(self, value: SampleInputT) -> bytes:
        if not (type(value) is (expected_type := self.value_type)):
            raise TypeError(f"{type(value)=} ≠ {expected_type=}")

        document = _JsonSamples(samples=tuple(value.samples.tolist()))

        return self._json_codec.encode(value=document)
