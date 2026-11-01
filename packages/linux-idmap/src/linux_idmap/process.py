"""Read stable, host-relative maps of an existing Linux process."""

from dataclasses import dataclass

from ._proc import HostProcfs, ProcessIdentity
from .maps import IdMap


@dataclass(frozen=True)
class ProcessMaps(ProcessIdentity):
    uid_map: IdMap
    gid_map: IdMap


def observe_process(pid: int) -> ProcessMaps:
    """Return maps only after host, process and map continuity are confirmed.

    The reader must be in the initial user namespace; PID is interpreted in
    its own visible procfs. Repeated observations are checked for changes,
    not claimed as an atomic snapshot. This never creates or stops a process.
    """
    with HostProcfs().process(pid) as process:
        uid_map, gid_map = process.read_maps()
        identity = process.identity
        return ProcessMaps(identity.pid, identity.starttime, identity.proc_device,
                           identity.proc_inode, uid_map, gid_map)
