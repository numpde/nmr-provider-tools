"""Declare fixture failure codes and an opaque binary result representation."""

from dataclasses import dataclass
from enum import auto

from provider_core.contribution_errors import ErrorCode


class FailureCode(ErrorCode):
    INPUT_REJECTED = auto()
    INVALID_INPUT = auto()
    INTERPRETER_FAILED = auto()
    INTERPRETER_UNAVAILABLE = auto()


@dataclass(frozen=True, slots=True)
class BytesCodec:
    """Keep protocol fixture bytes unchanged, including non-JSON results."""

    format: str

    @property
    def value_type(self) -> type[bytes]:
        return bytes

    def encode(self, value: bytes) -> bytes:
        return value

    def decode(self, payload: bytes) -> bytes:
        return payload


class CountingCodec:
    """Measure admission while allowing the caller to mutate its source later."""

    def __init__(self):
        self.current_format = "fixture.binary.v1"
        self.format_reads = 0
        self.encodes = 0

    @property
    def format(self) -> str:
        self.format_reads += 1
        return self.current_format

    @property
    def value_type(self) -> type[bytearray]:
        return bytearray

    def encode(self, value: bytearray) -> bytes:
        self.encodes += 1
        return bytes(value)

    def decode(self, payload: bytes) -> bytearray:
        return bytearray(payload)
