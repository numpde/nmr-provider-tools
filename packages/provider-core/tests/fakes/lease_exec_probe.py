"""Keep an unrelated executed process alive across scientific worker retirement."""

import os
import errno
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fakes.scientific_process import record_event


root = Path(sys.argv[1])
lease_identity = (int(sys.argv[2]), int(sys.argv[3]))
lease_present = False
for descriptor in list(Path("/proc/self/fd").iterdir()):
    number = int(descriptor.name)
    try:
        observed = os.fstat(number)
    except OSError as cause:
        if cause.errno != errno.EBADF:
            raise
        continue
    if (observed.st_dev, observed.st_ino) == lease_identity:
        lease_present = True
    elif number > 2:
        # The witness concerns only the lifetime lease, not other descriptor
        # inheritance or native channel lifetime.
        os.close(number)
record_event(root, "exec_probe_started", lease_present=lease_present)
deadline = time.monotonic() + 60
retirement_acknowledged = False
try:
    while not (root / "release-exec-probe").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("The independent exec probe was not released")
        if not retirement_acknowledged and (root / "worker-retired").exists():
            record_event(root, "exec_survived_retirement")
            retirement_acknowledged = True
        time.sleep(0.01)
finally:
    record_event(root, "exec_probe_done", released=(root / "release-exec-probe").exists())
