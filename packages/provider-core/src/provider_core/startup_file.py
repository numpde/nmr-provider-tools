"""Consume admitted startup bytes without later pathname reads.

Ports nmrnet-repro 29fb2f45d5a987b43767f782f88748dbeddc30c5,
nmrnet_provider/config.py::read_startup_file/_read_contents. Parent mounts remain
host-granted; the final descriptor owns regular-file, mode, size and close proof.
"""

from provider_core.container_guard import require_container

require_container()

import os
import stat
from pathlib import Path


class StartupFileRejected(ValueError):
    """Safe file-admission context; original read/close causes stay protected."""

    # silent-defaults: allow-contract -- Metadata refusal can precede a native error; independent close failures can follow either refusal or interruption.
    def __init__(self, message, *, cause=None, cleanup_failures=()):
        super().__init__(message)
        self.cause, self.cleanup_failures = cause, tuple(cleanup_failures)


# silent-defaults: allow-contract -- Public configuration and CA inputs forbid group/other writes; credentials additionally require private caller-owned 0400/0600 files.
def read_startup_file(path, *, maximum_bytes, label, secret=False):
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path) or not Path(path).is_absolute():
        raise StartupFileRejected(f"{label} requires an explicit absolute container path.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as cause:
        raise StartupFileRejected(f"{label} could not be opened as a regular file without a final symlink.", cause=cause) from None
    content, primary, cleanup = None, None, None
    try:
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
            raise StartupFileRejected(f"{label} must be a regular file within {maximum_bytes} bytes.")
        if secret:
            if status.st_uid != os.geteuid() or mode not in (0o400, 0o600):
                raise StartupFileRejected(f"{label} must belong to the container user with mode 0400 or 0600.")
        elif status.st_uid not in (0, os.geteuid()) or mode & 0o022:
            raise StartupFileRejected(f"{label} must belong to root or the container user and forbid group/other writes.")
        content = bytearray()
        while len(content) <= maximum_bytes:
            part = os.read(descriptor, maximum_bytes + 1 - len(content))
            if not part:
                break
            content.extend(part)
        if len(content) != status.st_size or len(content) > maximum_bytes:
            raise StartupFileRejected(f"{label} changed length during its bounded startup read.")
        content = bytes(content)
    except BaseException as cause:
        primary = cause
    try:
        os.close(descriptor)
    except OSError as cause:
        cleanup = cause
    if primary is not None and not isinstance(primary, Exception):
        if cleanup is not None:
            raise primary from StartupFileRejected(
                f"{label} close also failed during interruption.", cause=primary.__cause__, cleanup_failures=(cleanup,)
            )
        raise primary
    if primary is not None or cleanup is not None:
        if isinstance(primary, StartupFileRejected) and cleanup is None:
            raise primary
        # silent-defaults: allow-contract -- A successful close contributes no cleanup failure; the original read failure remains primary.
        cleanup_failures = () if cleanup is None else (cleanup,)
        raise StartupFileRejected(
            f"{label} could not be read and closed; no runtime resources were acquired.", cause=primary,
            cleanup_failures=cleanup_failures,
        ) from None
    return content
