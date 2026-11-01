"""Linux host-relative map evidence, not a permission-changing operation.

The guard follows the qualified NMRPeak/NMRNet nsfs + initial-inode checks,
strengthened with a pinned genuine procfs directory and calling-thread checks.
Initial namespace inode constants are Linux UAPI (include/uapi/linux/nsfs.h).
Identity-looking uid_map/gid_map contents alone are deliberately never evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import sys
import threading


# Linux nsfs ABI identity, checked before namespace ioctls.
# repo-check: allow-module-constant
_NSFS_MAGIC = 0x6E736673
# Linux procfs ABI identity, checked before process reads.
# repo-check: allow-module-constant
_PROCFS_MAGIC = 0x9FA0
# Linux UAPI initial user-namespace inode; identity maps are not a substitute.
# repo-check: allow-module-constant
_USER_INIT_INO = 4026531837
# Linux UAPI initial PID-namespace inode; process IDs must be host relative.
# repo-check: allow-module-constant
_PID_INIT_INO = 4026531836
# Linux nsfs ioctl ABI for namespace-type evidence.
# repo-check: allow-module-constant
_NS_GET_NSTYPE = 0xB703
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
# Linux proc stat states accepted as live process evidence.
# repo-check: allow-module-constant
_LIVE_STATES = frozenset("RSDTtWIKP")
# Linux proc stat states that establish process exit.
# repo-check: allow-module-constant
_DEAD_STATES = frozenset("ZXx")


class LinuxError(ValueError):
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
            raise LinuxError("invalid-map", "ID map has an invalid row count")
        for row in self.rows:
            if (type(row) is not IdRange
                    or any(type(value) is not int for value in (row.inside, row.outside, row.length))
                    or min(row.inside, row.outside) < 0 or row.length < 1
                    or max(row.inside + row.length, row.outside + row.length) > _MAX_ID + 1):
                raise LinuxError("invalid-map", "ID map range is invalid or overflows")
        for attribute in ("inside", "outside"):
            ordered = sorted(self.rows, key=lambda row: getattr(row, attribute))
            for left, right in zip(ordered, ordered[1:]):
                if getattr(left, attribute) + left.length > getattr(right, attribute):
                    raise LinuxError("invalid-map", "ID map ranges overlap")

    def translate(self, identifier: int) -> int:
        if type(identifier) is not int or not 0 <= identifier <= _MAX_ID:
            raise LinuxError("unmapped-id", "Requested ID is not a usable Linux identity")
        for row in self.rows:
            if row.inside <= identifier < row.inside + row.length:
                return row.outside + identifier - row.inside
        raise LinuxError("unmapped-id", "Requested ID has no host mapping")


def parse_id_map(raw: bytes) -> IdMap:
    if type(raw) is not bytes or len(raw) > _MAP_BYTES:
        raise LinuxError("invalid-map", "ID map exceeds its read bound")
    lines = raw.removesuffix(b"\n").split(b"\n")
    if not 1 <= len(lines) <= _MAX_MAP_ROWS:
        raise LinuxError("invalid-map", "ID map has an invalid row count")
    rows = []
    for line in lines:
        match = _MAP_ROW.fullmatch(line)
        if match is None or any(len(value) > 10 for value in match.groups()):
            raise LinuxError("invalid-map", "ID map row is not three ASCII decimals")
        rows.append(IdRange(*(int(value) for value in match.groups())))
    return IdMap(tuple(rows))


@dataclass(frozen=True)
class HostEvidence:
    user_namespace_inode: int
    pid_namespace_inode: int
    proc_device: int
    proc_inode: int
    reader_pid: int
    reader_tid: int


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    starttime: int
    proc_device: int
    proc_inode: int


@dataclass(frozen=True)
class ProcessObservation(ProcessIdentity):
    uid_map: IdMap
    gid_map: IdMap


def _filesystem_type(fd: int) -> int:
    # Linux struct statfs starts with a native long f_type. An aligned oversized
    # buffer avoids architecture-dependent layouts of the remaining fields.
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.fstatfs
    call.argtypes = [ctypes.c_int, ctypes.c_void_p]
    call.restype = ctypes.c_int
    buffer = (ctypes.c_long * 64)()
    if call(fd, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), "filesystem identity unavailable")
    return buffer[0]


@contextmanager
def _directory(path, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        if _filesystem_type(fd) != _PROCFS_MAGIC:
            raise LinuxError("host-precondition", "Map observation requires genuine procfs")
        yield fd
    finally:
        os.close(fd)


def _namespace_inode(root_fd: int, name: str) -> int:
    fd = os.open(f"thread-self/ns/{name}", os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd)
    try:
        expected_type = {"user": 0x10000000, "pid": 0x20000000}[name]
        if _filesystem_type(fd) != _NSFS_MAGIC or fcntl.ioctl(fd, _NS_GET_NSTYPE) != expected_type:
            raise LinuxError("host-precondition", "Namespace evidence is not the required nsfs type")
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limit: int) -> bytes:
    source = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if _filesystem_type(source) != _PROCFS_MAGIC:
            raise LinuxError("process-identity", "Process evidence is not genuine procfs")
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(source, limit + 1 - len(raw))
            if not part:
                return bytes(raw)
            raw.extend(part)
        raise LinuxError("process-identity", "Process evidence exceeds its read bound")
    finally:
        os.close(source)


def _parse_stat(raw: bytes, pid: int) -> tuple[str, int]:
    closing = raw.rfind(b") ")
    if not raw.startswith(str(pid).encode("ascii") + b" (") or closing < 0:
        raise LinuxError("process-identity", "Process status identity is malformed")
    fields = raw[closing + 2:].split()
    if (len(fields) < 20 or fields[0] not in {state.encode("ascii") for state in _LIVE_STATES | _DEAD_STATES}
            or _DECIMAL.fullmatch(fields[19]) is None or len(fields[19]) > 20):
        raise LinuxError("process-identity", "Process status fields are malformed")
    starttime = int(fields[19])
    if starttime > 0xFFFFFFFFFFFFFFFF:
        raise LinuxError("process-identity", "Process start time overflows")
    return fields[0].decode("ascii"), starttime


def _require_token(raw: bytes, token: str):
    if not raw.endswith(b"\0") or raw[:-1].split(b"\0").count(token.encode("ascii")) != 1:
        raise LinuxError("process-identity", "Probe process lacks its exact operation token")


def _validate_pid(pid: int):
    if type(pid) is not int or not 1 <= pid <= 0x7FFFFFFF:
        raise LinuxError("process-identity", "Probe has no valid host PID")


class LinuxGuard:
    """Observe one genuine local Linux procfs; unsafe namespace evidence fails closed."""

    # silent-defaults: allow-contract -- Linux process evidence comes from /proc unless a caller explicitly supplies a root; filesystem and namespace checks apply equally to both; tests/test_linux.py::HostGuardTests.
    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = Path(proc_root)

    def host_evidence(self) -> HostEvidence:
        if sys.platform != "linux":
            raise LinuxError("host-precondition", "Host ID observation requires Linux")
        try:
            with _directory(self.proc_root) as root:
                pid, tid = os.getpid(), threading.get_native_id()
                if (os.readlink("self", dir_fd=root) != str(pid)
                        or os.readlink("thread-self", dir_fd=root) != f"{pid}/task/{tid}"):
                    raise LinuxError("host-precondition", "procfs does not expose the calling host process")
                user = _namespace_inode(root, "user")
                pidns = _namespace_inode(root, "pid")
                if (user, pidns) != (_USER_INIT_INO, _PID_INIT_INO):
                    raise LinuxError("host-precondition", "Map reader must be in initial user and PID namespaces")
                metadata = os.fstat(root)
                return HostEvidence(user, pidns, metadata.st_dev, metadata.st_ino, pid, tid)
        except OSError as cause:
            raise LinuxError("host-precondition", "Host namespace evidence is unavailable") from cause

    def identify(self, pid: int, token: str) -> ProcessIdentity:
        """Pin a live owned process for cleanup without requiring readable ID maps."""
        return self._observe(pid, token, read_maps=False)

    def observe(self, pid: int, token: str) -> ProcessObservation:
        return self._observe(pid, token, read_maps=True)

    def _observe(self, pid: int, token: str, *, read_maps: bool):
        _validate_pid(pid)
        if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token) is None:
            raise LinuxError("process-identity", "Probe ownership token is invalid")
        host = self.host_evidence()
        try:
            with _directory(self.proc_root) as root, _directory(str(pid), dir_fd=root) as process:
                root_meta, process_meta = os.fstat(root), os.fstat(process)
                if (root_meta.st_dev, root_meta.st_ino) != (host.proc_device, host.proc_inode):
                    raise LinuxError("host-precondition", "procfs changed before process observation")
                state, starttime = _parse_stat(_read_at(process, "stat", 4096), pid)
                if state not in _LIVE_STATES:
                    raise LinuxError("process-identity", "Probe process is not live")
                _require_token(_read_at(process, "cmdline", 8192), token)
                if read_maps:
                    uid_map = parse_id_map(_read_at(process, "uid_map", _MAP_BYTES))
                    gid_map = parse_id_map(_read_at(process, "gid_map", _MAP_BYTES))
                    if (uid_map != parse_id_map(_read_at(process, "uid_map", _MAP_BYTES))
                            or gid_map != parse_id_map(_read_at(process, "gid_map", _MAP_BYTES))):
                        raise LinuxError("process-identity", "Probe ID maps changed during observation")
                _require_token(_read_at(process, "cmdline", 8192), token)
                after_state, after_starttime = _parse_stat(_read_at(process, "stat", 4096), pid)
                after_meta = os.stat(str(pid), dir_fd=root, follow_symlinks=False)
                if (after_state not in _LIVE_STATES or starttime != after_starttime
                        or (after_meta.st_dev, after_meta.st_ino) != (process_meta.st_dev, process_meta.st_ino)):
                    raise LinuxError("process-identity", "Probe PID was replaced or exited during observation")
            if host != self.host_evidence():
                raise LinuxError("host-precondition", "Host namespace evidence changed during observation")
            identity = (pid, starttime, process_meta.st_dev, process_meta.st_ino)
            return ProcessObservation(*identity, uid_map, gid_map) if read_maps else ProcessIdentity(*identity)
        except OSError as cause:
            raise LinuxError("process-identity", "Cannot establish stable host probe process evidence") from cause

    def pid_absent(self, pid: int) -> bool:
        """Conservative retirement proof when an exact identity was unavailable."""
        _validate_pid(pid)
        host = self.host_evidence()
        try:
            with _directory(self.proc_root) as root:
                metadata = os.fstat(root)
                if (metadata.st_dev, metadata.st_ino) != (host.proc_device, host.proc_inode):
                    raise LinuxError("host-precondition", "procfs changed before PID absence check")
                try:
                    os.stat(str(pid), dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    absent = True
                else:
                    absent = False
            if host != self.host_evidence():
                raise LinuxError("host-precondition", "host namespace changed before PID absence check")
            return absent
        except OSError as cause:
            raise LinuxError("process-identity", "probe PID absence cannot be established") from cause

    def process_retired(self, observation: ProcessIdentity) -> bool:
        """Whether this exact process exited; unreadable evidence is not absence."""
        _validate_pid(observation.pid)
        if (type(observation.starttime) is not int or not 0 <= observation.starttime <= 0xFFFFFFFFFFFFFFFF
                or type(observation.proc_device) is not int or observation.proc_device < 0
                or type(observation.proc_inode) is not int or observation.proc_inode <= 0):
            raise LinuxError("process-identity", "Probe retirement identity is invalid")
        host = self.host_evidence()
        try:
            with _directory(self.proc_root) as root:
                root_meta = os.fstat(root)
                if (root_meta.st_dev, root_meta.st_ino) != (host.proc_device, host.proc_inode):
                    raise LinuxError("host-precondition", "procfs changed before process retirement check")
                try:
                    with _directory(str(observation.pid), dir_fd=root) as process:
                        metadata = os.fstat(process)
                        state, starttime = _parse_stat(_read_at(process, "stat", 4096), observation.pid)
                except FileNotFoundError:
                    retired = True
                else:
                    retired = state in _DEAD_STATES or starttime != observation.starttime
                    if (not retired and (metadata.st_dev, metadata.st_ino)
                            != (observation.proc_device, observation.proc_inode)):
                        # proc inode eviction can change inode numbers while a
                        # PID remains alive: a mismatch is not proof of exit.
                        raise LinuxError("process-identity", "Live process inode changed before retirement check")
            if host != self.host_evidence():
                raise LinuxError("host-precondition", "Host namespace evidence changed during retirement check")
            return retired
        except OSError as cause:
            raise LinuxError("process-identity", "Probe process retirement cannot be established") from cause
