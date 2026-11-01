"""Pinned Linux process evidence for the core and its process-owning add-ons."""

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

from .maps import IdentityError, parse_id_map, _MAP_BYTES


# Linux nsfs ABI identity, checked before namespace ioctls.
# repo-check: allow-module-constant
_NSFS_MAGIC = 0x6E736673
# Linux procfs ABI identity, checked before process reads.
# repo-check: allow-module-constant
_PROCFS_MAGIC = 0x9FA0
# Linux UAPI initial user-namespace inode; identity maps are not a substitute.
# repo-check: allow-module-constant
_USER_INIT_INO = 4026531837
# Linux nsfs ioctl ABI for namespace-type evidence.
# repo-check: allow-module-constant
_NS_GET_NSTYPE = 0xB703
# Canonical process-stat decimal grammar.
# repo-check: allow-module-constant
_DECIMAL = re.compile(rb"(?:0|[1-9][0-9]*)\Z")
# Linux proc stat states accepted as live process evidence.
# repo-check: allow-module-constant
_LIVE_STATES = frozenset("RSDTtWIKP")
# Linux proc stat states that establish process exit.
# repo-check: allow-module-constant
_DEAD_STATES = frozenset("ZXx")


@dataclass(frozen=True)
class HostEvidence:
    user_namespace_inode: int
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
            raise IdentityError("host-precondition", "Map observation requires genuine procfs")
        yield fd
    finally:
        os.close(fd)


def _namespace_inode(root_fd: int, name: str) -> int:
    fd = os.open(f"thread-self/ns/{name}", os.O_RDONLY | os.O_CLOEXEC, dir_fd=root_fd)
    try:
        expected_type = {"user": 0x10000000, "pid": 0x20000000}[name]
        if _filesystem_type(fd) != _NSFS_MAGIC or fcntl.ioctl(fd, _NS_GET_NSTYPE) != expected_type:
            raise IdentityError("host-precondition", "Namespace evidence is not the required nsfs type")
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limit: int) -> bytes:
    source = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if _filesystem_type(source) != _PROCFS_MAGIC:
            raise IdentityError("process-identity", "Process evidence is not genuine procfs")
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(source, limit + 1 - len(raw))
            if not part:
                return bytes(raw)
            raw.extend(part)
        raise IdentityError("process-identity", "Process evidence exceeds its read bound")
    finally:
        os.close(source)


def _parse_stat(raw: bytes, pid: int) -> tuple[str, int]:
    closing = raw.rfind(b") ")
    if not raw.startswith(str(pid).encode("ascii") + b" (") or closing < 0:
        raise IdentityError("process-identity", "Process status identity is malformed")
    fields = raw[closing + 2:].split()
    if (len(fields) < 20 or fields[0] not in {state.encode("ascii") for state in _LIVE_STATES | _DEAD_STATES}
            or _DECIMAL.fullmatch(fields[19]) is None or len(fields[19]) > 20):
        raise IdentityError("process-identity", "Process status fields are malformed")
    starttime = int(fields[19])
    if starttime > 0xFFFFFFFFFFFFFFFF:
        raise IdentityError("process-identity", "Process start time overflows")
    return fields[0].decode("ascii"), starttime


def _validate_pid(pid: int):
    if type(pid) is not int or not 1 <= pid <= 0x7FFFFFFF:
        raise IdentityError("process-identity", "Process has no valid host PID")


class HostProcfs:
    """Observe one genuine local Linux procfs; unsafe namespace evidence fails closed."""

    # silent-defaults: allow-contract -- Linux process evidence comes from /proc unless a caller explicitly supplies a root; filesystem and namespace checks apply equally to both; tests/test_linux.py::HostGuardTests.
    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = Path(proc_root)

    def host_evidence(self) -> HostEvidence:
        if sys.platform != "linux":
            raise IdentityError("host-precondition", "Host ID observation requires Linux")
        try:
            with _directory(self.proc_root) as root:
                pid, tid = os.getpid(), threading.get_native_id()
                if (os.readlink("self", dir_fd=root) != str(pid)
                        or os.readlink("thread-self", dir_fd=root) != f"{pid}/task/{tid}"):
                    raise IdentityError("host-precondition", "procfs does not expose the calling host process")
                user = _namespace_inode(root, "user")
                if user != _USER_INIT_INO:
                    raise IdentityError("host-precondition", "Map reader must be in the initial user namespace")
                metadata = os.fstat(root)
                return HostEvidence(user, metadata.st_dev, metadata.st_ino, pid, tid)
        except OSError as cause:
            raise IdentityError("host-precondition", "Host namespace evidence is unavailable") from cause

    @contextmanager
    def root(self):
        """Keep each successful process read bound to the same host procfs."""
        host = self.host_evidence()
        with _directory(self.proc_root) as root:
            metadata = os.fstat(root)
            if (metadata.st_dev, metadata.st_ino) != (host.proc_device, host.proc_inode):
                raise IdentityError("host-precondition", "procfs changed before process evidence read")
            yield root
        if host != self.host_evidence():
            raise IdentityError("host-precondition", "Host namespace evidence changed during process evidence read")

    @contextmanager
    def process(self, pid: int):
        """Yield one live pinned process; normal exit proves it stayed the same."""
        _validate_pid(pid)
        try:
            with self.root() as root, _directory(str(pid), dir_fd=root) as fd:
                metadata = os.fstat(fd)
                state, starttime = _parse_stat(_read_at(fd, "stat", 4096), pid)
                if state not in _LIVE_STATES:
                    raise IdentityError("process-identity", "Process is not live")
                identity = ProcessIdentity(pid, starttime, metadata.st_dev, metadata.st_ino)
                yield ProcessDirectory(fd, identity)
                state, after_starttime = _parse_stat(_read_at(fd, "stat", 4096), pid)
                after = os.stat(str(pid), dir_fd=root, follow_symlinks=False)
                if (state not in _LIVE_STATES or starttime != after_starttime
                        or (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)):
                    raise IdentityError("process-identity", "Process was replaced or exited during observation")
        except OSError as cause:
            raise IdentityError("process-identity", "Cannot establish stable host process evidence") from cause


@dataclass(frozen=True)
class ProcessDirectory:
    """A bounded reader valid only inside HostProcfs.process()."""

    fd: int
    identity: ProcessIdentity

    def read(self, name: str, limit: int) -> bytes:
        return _read_at(self.fd, name, limit)

    def read_maps(self):
        uid_map = parse_id_map(self.read("uid_map", _MAP_BYTES))
        gid_map = parse_id_map(self.read("gid_map", _MAP_BYTES))
        if (uid_map != parse_id_map(self.read("uid_map", _MAP_BYTES))
                or gid_map != parse_id_map(self.read("gid_map", _MAP_BYTES))):
            raise IdentityError("process-identity", "ID maps changed during observation")
        return uid_map, gid_map
