"""Private append-only recovery evidence, made durable before Docker mutation."""

import json
import os
from pathlib import Path
import stat

from .model import StepError


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.fd = -1
        self.directory = -1
        try:
            self.directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent = os.fstat(self.directory)
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise StepError("journal", "journal parent must be caller-owned and private (0700)")
            self.fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=self.directory)
            os.fchmod(self.fd, 0o600)
            os.fsync(self.directory)
        except BaseException:
            self.close()
            raise

    def append(self, event: dict) -> None:
        encoded = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 16384:
            raise StepError("journal", "recovery event exceeded its size bound")
        view = memoryview(encoded)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise StepError("journal", "recovery evidence write failed")
            view = view[written:]
        os.fsync(self.fd)

    def close(self) -> None:
        for name in ("fd", "directory"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
