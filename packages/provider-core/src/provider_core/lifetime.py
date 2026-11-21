"""Keep the existing local supervisor lock independently of checkpoint storage."""

from provider_core.container_guard import require_container

require_container()

import fcntl
import os
import pathlib
import reprlib
import stat

from provider_core.type_requirements import require_instance


class LifetimeRefused(RuntimeError):
    """No lifetime was acquired; preserve the existing owner."""


class ProviderLifetime:
    """Exclude another local supervisor before API effects.

    Composition closes this only after its original API, delivery and native
    operations settle. Closing the lock does not itself prove that settlement.
    The existing filename also excludes providers still using CheckpointStore.
    A close failure leaves release unconfirmed and permanently blocks reopening
    this instance; later close calls report the original failure without retrying.
    """

    def __init__(self, root):
        require_instance(root, pathlib.Path)

        if not root.is_absolute():
            raise ValueError(f"{root=}; expected an absolute path")

        (self._root, self._lock_fd) = (root, None)
        self._close_failure = None

    def open(self):
        # repo-check: allow-literal-guard-error -- The chained original close failure supplies evidence; reopening cannot establish lock release
        if not (self._close_failure is None):
            raise LifetimeRefused(
                "Cannot reopen provider lifetime after its lock close failed; release remains unconfirmed."
            ) from self._close_failure

        if not (self._lock_fd is None):
            raise LifetimeRefused(f"{self._lock_fd=} should be None before opening the provider lifetime")

        directory_metadata = self._root.lstat()
        canonical_root = self._root.resolve()

        if not (canonical_root == self._root):
            raise LifetimeRefused(f"{reprlib.repr(self._root)=} should match {reprlib.repr(canonical_root)=}")

        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise LifetimeRefused(f"{reprlib.repr(self._root)=} should reference a directory; {directory_metadata.st_mode=:#o}")

        expected_uid = os.geteuid()

        if not (directory_metadata.st_uid == expected_uid):
            raise LifetimeRefused(f"{directory_metadata.st_uid=} should match {expected_uid=}")

        expected_directory_mode = 0o700

        if not (stat.S_IMODE(directory_metadata.st_mode) == expected_directory_mode):
            raise LifetimeRefused(f"{stat.S_IMODE(directory_metadata.st_mode)=:#o} should match {expected_directory_mode=:#o}")

        self._lock_fd = os.open(
            self._root / ".checkpoint-owner.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )

        try:
            lock_metadata = os.fstat(self._lock_fd)

            if not stat.S_ISREG(lock_metadata.st_mode):
                raise LifetimeRefused(f"{self._lock_fd=} should reference a regular file; {lock_metadata.st_mode=:#o}")

            expected_uid = os.geteuid()

            if not (lock_metadata.st_uid == expected_uid):
                raise LifetimeRefused(f"{lock_metadata.st_uid=} should match {expected_uid=}")

            expected_lock_mode = 0o600

            if not (stat.S_IMODE(lock_metadata.st_mode) == expected_lock_mode):
                raise LifetimeRefused(f"{stat.S_IMODE(lock_metadata.st_mode)=:#o} should match {expected_lock_mode=:#o}")

            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as failure:
            try:
                self.close()
            except BaseException as cleanup_failure:
                raise BaseExceptionGroup(
                    "Provider lifetime admission failed and lock release is unconfirmed.",
                    [failure, cleanup_failure],
                ) from None

            raise

        return self

    def close(self):
        if not (self._close_failure is None):
            raise self._close_failure

        if self._lock_fd is not None:
            (lock_fd, self._lock_fd) = (self._lock_fd, None)

            try:
                os.close(lock_fd)
            except BaseException as failure:
                self._close_failure = failure

                raise
