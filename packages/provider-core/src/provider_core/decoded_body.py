"""Decode a bounded HTTP content-coding stack without owning I/O or retries.

Only complete gzip members are accepted, including concatenation. Every declared
coding has its own aggregate output cap; no stage calls unbounded zlib flush.
Deflate selects its wrapper from the first two bytes. A valid RFC1950 header
commits to zlib; errors never trigger a raw retry, including raw streams that
coincidentally begin with that header. Other prefixes select raw deflate.
The transport retains response headers, chooses the status-specific limit and
owns wire accounting, cancellation and physical exchange retirement.
"""

from provider_core.container_guard import require_container

require_container()

import sys
import zlib
import reprlib

from provider_core.type_requirements import require_type
from provider_core.validation.upstream.content_encoding import read_content_codings


# Closed diagnostic reasons belong to the supported codec profile.
# repo-check: allow-module-constant
_MESSAGES = {
    "size_limit": "HTTP response exceeded its configured encoded or decoded byte limit.",
    "invalid_encoding": "HTTP response content encoding is incomplete or invalid.",
    "unsupported_encoding": "HTTP response content encoding is outside the supported decoding profile.",
}

# This fixed profile bounds intermediate decoding work, rather than operator policy.
# repo-check: allow-module-constant
_MAX_CODINGS = 4


class DecodedBodyRejected(ValueError):
    """A closed safe reason; native codec evidence remains the protected cause."""

    def __init__(self, reason):
        require_type(reason, str)

        if not (reason in _MESSAGES):
            expected_reasons = tuple(_MESSAGES)

            raise ValueError(f"{reprlib.repr(reason)=} ∉ {expected_reasons=}")

        self.reason = reason

        super().__init__(_MESSAGES[reason])


class _Budget:
    def __init__(self, maximum_bytes):
        self.remaining = maximum_bytes

    def accept(self, decoded_bytes):
        if len(decoded_bytes) > self.remaining:
            raise DecodedBodyRejected("size_limit")

        self.remaining -= len(decoded_bytes)

        return decoded_bytes

    def decompress(self, decoder, encoded_bytes):
        try:
            output = decoder.decompress(encoded_bytes, self.remaining + 1)
        except zlib.error as cause:
            raise DecodedBodyRejected("invalid_encoding") from cause

        return self.accept(output)


class _Identity:
    def __init__(self, maximum_bytes):
        self._budget = _Budget(maximum_bytes)

    def feed(self, chunk_bytes):
        return self._budget.accept(chunk_bytes)

    def finish(self):
        return None


class _Gzip:
    def __init__(self, maximum_bytes):
        self._budget = _Budget(maximum_bytes)
        self._decoder = None
        self._members = 0

    def feed(self, encoded_bytes):
        output = []

        while encoded_bytes:
            if self._decoder is None:
                self._decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)

            output.append(self._budget.decompress(self._decoder, encoded_bytes))

            if not self._decoder.eof:
                break

            # A non-rejected call has not reached the output sentinel. At EOF,
            # only unused_data remains, and belongs to another complete member.
            encoded_bytes = self._decoder.unused_data

            self._decoder = None

            self._members += 1

        return b"".join(output)

    def finish(self):
        if (self._members == 0) or (self._decoder is not None):
            raise DecodedBodyRejected("invalid_encoding")


class _Deflate:
    def __init__(self, maximum_bytes):
        self._budget = _Budget(maximum_bytes)
        self._prefix = b""
        self._decoder = None

    def feed(self, encoded_bytes):
        if self._decoder is None:
            encoded_bytes = self._prefix + encoded_bytes

            if len(encoded_bytes) < 2:
                self._prefix = encoded_bytes

                return b""

            self._prefix = b""

            (first, second) = (encoded_bytes[0], encoded_bytes[1])

            wrapped = (
                (first & 15 == 8) and
                (first >> 4 <= 7) and
                (((first << 8) | second) % 31 == 0)
            )

            self._decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)

        output = self._budget.decompress(self._decoder, encoded_bytes)

        if self._decoder.unused_data:
            raise DecodedBodyRejected("invalid_encoding")

        return output

    def finish(self):
        if (self._decoder is None) or (not self._decoder.eof):
            raise DecodedBodyRejected("invalid_encoding")


def require_decoded_limit(maximum_bytes):
    """Admit the sentinel allowance before the transport dispatches any I/O."""

    require_type(maximum_bytes, int)

    (min_decoded_bytes, max_decoded_bytes) = (1, sys.maxsize - 1)

    if not (min_decoded_bytes <= maximum_bytes <= max_decoded_bytes):
        raise ValueError(f"{maximum_bytes=} should be between {min_decoded_bytes=} and {max_decoded_bytes=}")


class DecodedBody:
    """Feed one entity, then verify completion; any failed call poisons reuse."""

    def __init__(self, headers, *, maximum_bytes):
        require_decoded_limit(maximum_bytes)
        constructors = {b"identity": _Identity, b"gzip": _Gzip, b"deflate": _Deflate}
        codings = read_content_codings(headers, supported_codings=constructors.keys(), max_codings=_MAX_CODINGS)

        if not codings:
            raise DecodedBodyRejected("unsupported_encoding")

        # repo-check: allow-tuple-generator -- The admitted decoding stages stay fixed for this entity.
        self._stages = tuple(constructors[coding](maximum_bytes) for coding in codings)

        self._decoder_state = "open"

    def _require_open(self):
        if not (self._decoder_state == (expected_decoder_state := "open")):
            raise RuntimeError(f"{self._decoder_state=} should equal {expected_decoder_state=}")

    def feed(self, data):
        self._require_open()

        try:
            for stage in self._stages:
                data = stage.feed(data)

            return data
        except BaseException:
            self._decoder_state = "failed"

            raise

    def finish(self):
        self._require_open()

        try:
            for stage in self._stages:
                stage.finish()
        except BaseException:
            self._decoder_state = "failed"

            raise

        self._decoder_state = "finished"
