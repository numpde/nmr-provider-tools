"""Keep the existing local supervisor lock independently of checkpoint storage."""

from provider_core.container_guard import require_container

require_container()

import fcntl
import os
from pathlib import Path
import stat


class LifetimeRefused(RuntimeError):
    """No lifetime was acquired; preserve the existing owner."""


class ProviderLifetime:
    """Exclude another local supervisor before API effects.

    Composition closes this only after its original API, delivery and native
    operations settle. Closing the lock does not itself prove that settlement.
    The existing filename also excludes providers still using CheckpointStore.
    """

    def __init__(self, root):
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("Provider lifetime requires an absolute private root.")
        self._root, self._descriptor = root, None

    def open(self):
        if self._descriptor is not None:
            raise LifetimeRefused("Provider lifetime already holds its lock; retain its original owner.")
        facts = self._root.lstat()
        if (self._root.resolve() != self._root or not stat.S_ISDIR(facts.st_mode)
                or facts.st_uid != os.geteuid() or stat.S_IMODE(facts.st_mode) != 0o700):
            raise LifetimeRefused("Provider lifetime directory must be canonical, caller-owned and mode 0700.")
        self._descriptor = os.open(
            self._root / ".checkpoint-owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        try:
            facts = os.fstat(self._descriptor)
            if (not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.geteuid()
                    or stat.S_IMODE(facts.st_mode) != 0o600):
                raise LifetimeRefused("Provider lifetime lock must be a caller-owned mode 0600 regular file.")
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
