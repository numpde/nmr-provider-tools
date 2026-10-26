"""Regular-file and durable-directory primitives for owned artifact operations."""

from tools.container_guard import require_container

require_container()

import os
import re
import stat
from contextlib import contextmanager


@contextmanager
def regular_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Cannot read artifact {path}: expected a regular file without a symlink.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_download_proxy():
    """Refuse ambient direct-network and proxy-bypass choices in fetch workers."""
    proxy = os.environ.get("HTTPS_PROXY")
    if not (
        proxy is not None
        and (match := re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", proxy)) is not None
        and 1 <= int(match[1]) <= 65535
        and all(os.environ.get(name) == proxy for name in ("HTTP_PROXY", "http_proxy", "https_proxy"))
        and all(os.environ.get(name) in (None, "") for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"))
    ):
        raise ValueError("Cannot acquire artifacts: launcher-owned loopback proxy configuration is required.")


