"""Read stable, host-relative maps of an existing Linux process."""

from dataclasses import dataclass

from .host import HostMap, qualified_process, ProcessIdentity


@dataclass(frozen=True)
class ProcessObservation(ProcessIdentity):
    uid_map: HostMap
    gid_map: HostMap


def observe_host_process(pid: int) -> ProcessObservation:
    """Return maps only after host, process and map continuity are confirmed.

    The reader must be in the initial user namespace; PID is interpreted in
    its own visible procfs. Repeated observations are checked for changes,
    not claimed as an atomic snapshot. This never creates or stops a process.
    """
    with qualified_process(pid) as process:
        maps = process.read_maps()
        identity = process.identity
        return ProcessObservation(identity.pid, identity.starttime, identity.proc_device,
                                  identity.proc_inode, HostMap(maps.uid_map), HostMap(maps.gid_map))
