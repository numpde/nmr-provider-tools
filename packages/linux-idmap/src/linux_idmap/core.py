"""Kernel-reported inside/outside ID maps, without host or consistency claims."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class IdRange:
    inside: int
    outside: int
    length: int


@dataclass(frozen=True)
class IdMap:
    """Caller-asserted usable affine rows in the intended outside coordinates.

    Construction establishes neither row validity nor source provenance.
    """

    rows: tuple[IdRange, ...]

    def translate(self, identifier: int) -> int:
        if type(identifier) is not int or not 0 <= identifier < 2**32 - 1:
            raise ValueError("identifier is not a usable Linux ID")
        for row in self.rows:
            if row.inside <= identifier < row.inside + row.length:
                outside = row.outside + identifier - row.inside
                if type(outside) is not int or not 0 <= outside < 2**32 - 1:
                    raise ValueError("outside identifier is not a usable Linux ID")
                return outside
        raise ValueError("identifier is not mapped")


@dataclass(frozen=True)
class Maps:
    uid_map: tuple[IdRange, ...]
    gid_map: tuple[IdRange, ...]


def parse_map(raw: bytes) -> tuple[IdRange, ...]:
    rows = []
    for line in raw.splitlines():
        inside, outside, length = map(int, line.split())
        rows.append(IdRange(inside, outside, length))
    return tuple(rows)


def read_maps(read_file: Callable[[str], bytes]) -> Maps:
    """Read uid_map and gid_map through one explicit file-reader boundary."""
    return Maps(parse_map(read_file("uid_map")), parse_map(read_file("gid_map")))


def observe_process(pid: int) -> Maps:
    """Sample visible /proc files; outside IDs follow the kernel's namespace rules."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    directory = Path("/proc") / str(pid)
    return read_maps(lambda name: (directory / name).read_bytes())
