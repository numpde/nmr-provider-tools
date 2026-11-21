"""Keep an unrelated executed process alive across scientific worker retirement."""

import os
import errno
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from tests.fakes.scientific_process import record_event


root = pathlib.Path(sys.argv[1])
lease_identity = (int(sys.argv[2]), int(sys.argv[3]))
lease_present = False

for descriptor_entry in list(pathlib.Path("/proc/self/fd").iterdir()):
    inherited_fd = int(descriptor_entry.name)

    try:
        observed = os.fstat(inherited_fd)
    except OSError as cause:
        if not (cause.errno == errno.EBADF):
            raise

        continue

    if (observed.st_dev, observed.st_ino) == lease_identity:
        lease_present = True
    elif inherited_fd > 2:
        # The witness concerns only the lifetime lease, not other descriptor
        # inheritance or native channel lifetime
        os.close(inherited_fd)

record_event(root, "exec_probe_started", lease_present=lease_present)
deadline = time.monotonic() + 60
retirement_acknowledged = False

try:
    while not (root / "release-exec-probe").exists():
        if not ((observed_monotonic := time.monotonic()) < deadline):
            raise TimeoutError(f"{observed_monotonic=} should be before {deadline=} while waiting for exec probe release")

        if (not retirement_acknowledged) and (root / "worker-retired").exists():
            record_event(root, "exec_survived_retirement")
            retirement_acknowledged = True

        time.sleep(0.01)
finally:
    record_event(root, "exec_probe_done", released=(root / "release-exec-probe").exists())
