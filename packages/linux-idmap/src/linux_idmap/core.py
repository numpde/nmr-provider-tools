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
    rows: tuple[IdRange, ...]

    def translate(self, identifier: int) -> int:
        if type(identifier) is not int or not 0 <= identifier < 2**32 - 1:
            raise ValueError("identifier is not a usable Linux ID")
        for row in self.rows:
            if row.inside <= identifier < row.inside + row.length:
                return row.outside + identifier - row.inside
        raise ValueError("identifier is not mapped")


@dataclass(frozen=True)
class Maps:
    uid_map: IdMap
    gid_map: IdMap


def parse_map(raw: bytes) -> IdMap:
    rows = []
    for line in raw.splitlines():
        inside, outside, length = (int(column) for column in line.split())
        rows.append(IdRange(inside, outside, length))
    return IdMap(tuple(rows))


def read_maps(read_file: Callable[[str], bytes]) -> Maps:
    """Read uid_map and gid_map through one explicit file-reader boundary."""
    return Maps(parse_map(read_file("uid_map")), parse_map(read_file("gid_map")))


def observe_process(pid: int) -> Maps:
    """Sample visible /proc files; outside IDs follow the kernel's namespace rules."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    directory = Path("/proc") / str(pid)
    return read_maps(lambda name: (directory / name).read_bytes())
