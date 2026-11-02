"""Read host-relative maps with process and reader checks."""

from dataclasses import dataclass

from ..core import IdMap
from .host import qualified_process, ProcessIdentity


@dataclass(frozen=True)
class ProcessObservation:
    """Reusable maps and the process identity checked while reading them."""

    identity: ProcessIdentity
    uid_map: IdMap
    gid_map: IdMap


def observe_host_process(pid: int) -> ProcessObservation:
    """Return maps only after host, process and map continuity are confirmed.

    The reader must be in the initial user namespace; PID is interpreted in
    its own visible procfs. Repeated observations are checked for changes,
    not claimed as an atomic snapshot. This never creates or stops a process.
    """
    with qualified_process(pid) as process:
        maps = process.read_maps()
        return ProcessObservation(process.identity, IdMap(maps.uid_rows), IdMap(maps.gid_rows))
