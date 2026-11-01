"""Owned-process and retirement evidence used only by the managed-probe add-on."""

import os
import re

from . import host as _proc
from .host import Procfs, ProcessIdentity, require_host, _directory, _parse_stat, _validate_pid, _DEAD_STATES
from .errors import IdentityError
from .observation import ProcessObservation


def _require_token(raw: bytes, token: str):
    if not raw.endswith(b"\0") or raw[:-1].split(b"\0").count(token.encode("ascii")) != 1:
        raise IdentityError("process-identity", "Probe process lacks its exact operation token")


# Docker reports host PIDs; a nested PID reader would inspect another process.
# repo-check: allow-module-constant
_HOST_PID_NAMESPACE = 4026531836


class ProbeProcesses(Procfs):
    def reader_evidence(self):
        host = super().reader_evidence()
        require_host(host)
        with _directory(self.proc_root) as root:
            if _proc._namespace_inode(root, "pid") != _HOST_PID_NAMESPACE:
                raise IdentityError("host-precondition", "Docker map reader must be in the initial PID namespace")
        return host

    def identify(self, pid: int, token: str) -> ProcessIdentity:
        return self._observe(pid, token, read_maps=False)

    def observe(self, pid: int, token: str) -> ProcessObservation:
        return self._observe(pid, token, read_maps=True)

    def _observe(self, pid: int, token: str, *, read_maps: bool):
        if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token) is None:
            raise IdentityError("process-identity", "Probe ownership token is invalid")
        with self.process(pid) as process:
            _require_token(process.read("cmdline", 8192), token)
            if read_maps:
                maps = process.read_maps()
            _require_token(process.read("cmdline", 8192), token)
            identity = process.identity
            return (ProcessObservation(identity.pid, identity.starttime, identity.proc_device,
                                identity.proc_inode, maps.uid_map, maps.gid_map) if read_maps else identity)

    def pid_absent(self, pid: int) -> bool:
        """Conservative retirement proof when an exact identity was unavailable."""
        _validate_pid(pid)
        try:
            with self.root() as (root, _reader):
                try:
                    os.stat(str(pid), dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                return False
        except OSError as cause:
            raise IdentityError("process-identity", "probe PID absence cannot be established") from cause

    def process_retired(self, observation: ProcessIdentity) -> bool:
        """Whether this exact process exited; unreadable evidence is not absence."""
        _validate_pid(observation.pid)
        if (type(observation.starttime) is not int or not 0 <= observation.starttime <= 0xFFFFFFFFFFFFFFFF
                or type(observation.proc_device) is not int or observation.proc_device < 0
                or type(observation.proc_inode) is not int or observation.proc_inode <= 0):
            raise IdentityError("process-identity", "Probe retirement identity is invalid")
        try:
            with self.root() as (root, _reader):
                try:
                    with _directory(str(observation.pid), dir_fd=root) as process:
                        metadata = os.fstat(process)
                        state, starttime = _parse_stat(_proc._read_at(process, "stat", 4096), observation.pid)
                except FileNotFoundError:
                    retired = True
                else:
                    retired = state in _DEAD_STATES or starttime != observation.starttime
                    if (not retired and (metadata.st_dev, metadata.st_ino)
                            != (observation.proc_device, observation.proc_inode)):
                        # proc inode eviction can change inode numbers while a
                        # PID remains alive: a mismatch is not proof of exit.
                        raise IdentityError("process-identity", "Live process inode changed before retirement check")
            return retired
        except OSError as cause:
            raise IdentityError("process-identity", "Probe process retirement cannot be established") from cause
