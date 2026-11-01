"""Validated Linux ID maps and inside-to-host translation."""

from dataclasses import dataclass
import re


# Linux uid_t/gid_t all-ones sentinel is not an assignable identity.
# repo-check: allow-module-constant
_MAX_ID = 4294967294  # (uid_t)-1 / (gid_t)-1 is not a usable identity.
# Bounded parser/read contract; test_linux.MapTests.test_row_count_and_byte_bounds.
# repo-check: allow-module-constant
_MAP_BYTES = 65536
# Linux map row-count bound; test_linux.MapTests.test_row_count_and_byte_bounds.
# repo-check: allow-module-constant
_MAX_MAP_ROWS = 340
# Canonical ASCII decimal grammar for procfs evidence.
# repo-check: allow-module-constant
_DECIMAL = re.compile(rb"(?:0|[1-9][0-9]*)\Z")
# Exact three-column Linux ID-map grammar; malformed rows fail closed.
# repo-check: allow-module-constant
_MAP_ROW = re.compile(rb"[ \t]*(0|[1-9][0-9]*)[ \t]+(0|[1-9][0-9]*)[ \t]+(0|[1-9][0-9]*)[ \t]*\Z")


class IdentityError(ValueError):
    """Sanitized failure with a stable category for the owning operation."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class IdRange:
    inside: int
    outside: int
    length: int


@dataclass(frozen=True)
class IdMap:
    rows: tuple[IdRange, ...]

    def __post_init__(self):
        if type(self.rows) is not tuple or not 1 <= len(self.rows) <= _MAX_MAP_ROWS:
            raise IdentityError("invalid-map", "ID map has an invalid row count")
        for row in self.rows:
            if (type(row) is not IdRange
                    or any(type(value) is not int for value in (row.inside, row.outside, row.length))
                    or min(row.inside, row.outside) < 0 or row.length < 1
                    or max(row.inside + row.length, row.outside + row.length) > _MAX_ID + 1):
                raise IdentityError("invalid-map", "ID map range is invalid or overflows")
        for attribute in ("inside", "outside"):
            ordered = sorted(self.rows, key=lambda row: getattr(row, attribute))
            for left, right in zip(ordered, ordered[1:]):
                if getattr(left, attribute) + left.length > getattr(right, attribute):
                    raise IdentityError("invalid-map", "ID map ranges overlap")

    def translate(self, identifier: int) -> int:
        if type(identifier) is not int or not 0 <= identifier <= _MAX_ID:
            raise IdentityError("unmapped-id", "Requested ID is not a usable Linux identity")
        for row in self.rows:
            if row.inside <= identifier < row.inside + row.length:
                return row.outside + identifier - row.inside
        raise IdentityError("unmapped-id", "Requested ID has no host mapping")


def parse_id_map(raw: bytes) -> IdMap:
    if type(raw) is not bytes or len(raw) > _MAP_BYTES:
        raise IdentityError("invalid-map", "ID map exceeds its read bound")
    lines = raw.removesuffix(b"\n").split(b"\n")
    if not 1 <= len(lines) <= _MAX_MAP_ROWS:
        raise IdentityError("invalid-map", "ID map has an invalid row count")
    rows = []
    for line in lines:
        match = _MAP_ROW.fullmatch(line)
        if match is None or any(len(value) > 10 for value in match.groups()):
            raise IdentityError("invalid-map", "ID map row is not three ASCII decimals")
        rows.append(IdRange(*(int(value) for value in match.groups())))
    return IdMap(tuple(rows))


