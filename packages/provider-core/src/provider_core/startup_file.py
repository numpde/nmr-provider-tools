"""Consume admitted startup bytes without later pathname reads.

Ports nmrnet-repro 29fb2f45d5a987b43767f782f88748dbeddc30c5,
nmrnet_provider/config.py::read_startup_file/_read_contents. Parent mounts remain
host-granted; the final descriptor owns regular-file, mode, size and close proof.
"""

from provider_core.container_guard import require_container

require_container()

import os
import stat

from .tools import require_absolute_path


class StartupFileRejected(ValueError):
    """Safe file-admission context; original read/close causes stay protected."""

    # silent-defaults: allow-contract -- Metadata refusal can precede a native error; independent close failures can follow either refusal or interruption.
    def __init__(self, message, *, cause=None, cleanup_failures=()):
        super().__init__(message)
        (self.cause, self.cleanup_failures) = (cause, tuple(cleanup_failures))


# silent-defaults: allow-contract -- Public configuration and CA inputs forbid group/other writes; credentials additionally require private caller-owned 0400/0600 files.
def read_startup_file(path, *, maximum_bytes, secret=False):
    try:
        path = require_absolute_path(path)
    except (TypeError, ValueError) as cause:
        raise StartupFileRejected("Cannot admit a startup file path", cause=cause) from None

    try:
        startup_file_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as cause:
        raise StartupFileRejected(f"{path=} could not be opened as a regular file without a final symlink.", cause=cause) from None

    (startup_bytes, read_failure, close_failure) = (None, None, None)

    try:
        file_stat = os.fstat(startup_file_fd)
        file_mode = file_stat.st_mode
        permissions = stat.S_IMODE(file_mode)

        if not stat.S_ISREG(file_mode):
            raise StartupFileRejected(f"{path=} should be a regular file; {file_mode=:#o}")

        # Negating > preserves the existing handling of unordered bounds such as NaN
        if not (not (file_stat.st_size > maximum_bytes)):
            raise StartupFileRejected(f"{path=}: {file_stat.st_size=} exceeds {maximum_bytes=}")

        if secret:
            expected_owner_id = os.geteuid()

            if not (file_stat.st_uid == expected_owner_id):
                raise StartupFileRejected(f"{path=}: {file_stat.st_uid=} should match {expected_owner_id=}")

            allowed_permissions = (0o400, 0o600)

            if not (permissions in allowed_permissions):
                allowed_permission_modes = [oct(permission) for permission in allowed_permissions]

                raise StartupFileRejected(f"{path=}: {permissions=:#o} ∉ {allowed_permission_modes=}")
        else:
            allowed_owner_ids = (0, os.geteuid())

            if not (file_stat.st_uid in allowed_owner_ids):
                raise StartupFileRejected(f"{path=}: {file_stat.st_uid=} ∉ {allowed_owner_ids=}")

            if not ((permissions & 0o022) == 0):
                raise StartupFileRejected(f"{path=}: {permissions=:#o} should forbid group and other writes")

        startup_bytes = bytearray()

        while len(startup_bytes) <= maximum_bytes:
            part = os.read(startup_file_fd, maximum_bytes + 1 - len(startup_bytes))

            if not part:
                break

            startup_bytes.extend(part)

        if not ((len(startup_bytes) == file_stat.st_size) and (not (len(startup_bytes) > maximum_bytes))):
            raise StartupFileRejected(f"{path=}: {len(startup_bytes)=} should match {file_stat.st_size=} within {maximum_bytes=}")

        startup_bytes = bytes(startup_bytes)
    except BaseException as cause:
        read_failure = cause

    try:
        os.close(startup_file_fd)
    except OSError as cause:
        close_failure = cause

    if not ((read_failure is None) or isinstance(read_failure, Exception)):
        if not (close_failure is None):
            raise read_failure from StartupFileRejected(
                f"{path=} close also failed during interruption.",
                cause=read_failure.__cause__,
                cleanup_failures=(close_failure,)
            )

        raise read_failure

    if not ((read_failure is None) and (close_failure is None)):
        if not ((not isinstance(read_failure, StartupFileRejected)) or (close_failure is not None)):
            raise read_failure

        # silent-defaults: allow-contract -- A successful close contributes no cleanup failure; the original read failure remains primary.
        cleanup_failures = () if close_failure is None else (close_failure,)

        raise StartupFileRejected(
            f"{path=} could not be read and closed; no runtime resources were acquired.",
            cause=read_failure,
            cleanup_failures=cleanup_failures,
        ) from None

    return startup_bytes
