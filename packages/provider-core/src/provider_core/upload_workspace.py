"""Keep acquired source files alive until their I/O and readers retire."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import os
from pathlib import Path
import shutil
from types import MappingProxyType

from provider_core.upload_file import UploadFailed
from provider_core.tools import require_positive_number


class UploadWorkspace:
    """Own one fresh private directory around a JobUploads acquisition session.

    Call open before acquiring files. The parent directory must be private to the caller;
    an existing workspace is never adopted or removed. Receipts stay available
    through retirement. Scientific selection and cache reuse remain caller policy.

    retire_readers is an async operation supplied by composition. It must join
    every admitted scientific reader, or raise if quiescence cannot be confirmed.
    New reader admission must stop before retire is called. This owner serializes
    its acquisitions against retirement; direct operations on the supplied
    JobUploads must also have finished before retirement.
    """

    def __init__(self, directory, *, uploads, retirement_seconds, retire_readers):
        require_positive_number(retirement_seconds)
        self._directory = Path(directory).absolute()
        self.uploads = uploads
        self._retirement_seconds = retirement_seconds
        self._retire_readers = retire_readers
        self._directory_fd = None
        self._created = False
        self._retiring = False
        self._retired = False
        self._close_failure = None
        self._receipts = {}
        self._operation = asyncio.Lock()

    @property
    def directory(self):
        """Absolute workspace path passed to supervised readers."""

        return self._directory

    @property
    def receipts(self):
        """A read-only view of successful transfers, keyed by Upload identity."""

        return MappingProxyType(self._receipts)

    def open(self):
        """Create exclusively; retained files require their original owner's cleanup."""

        if self._created or self._retiring:
            raise UploadFailed("Cannot open a source workspace more than once")

        self.directory.mkdir(mode=0o700)
        self._created = True
        self._directory_fd = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )

    async def acquire(self, upload_ref):
        """Delegate transfer policy to JobUploads and retain its successful receipt."""

        async with self._operation:
            if self._directory_fd is None or self._retiring:
                raise UploadFailed("Cannot acquire a source outside an open workspace")

            receipt = await self.uploads.acquire_file(
                upload_ref, directory_fd=self._directory_fd,
                retirement_seconds=self._retirement_seconds,
            )
            self._receipts[receipt.upload_ref] = receipt
            return receipt

    async def retire(self):
        """Join readers and I/O before deletion; retain files on any uncertainty.

        Failed retirement can be retried. A failed descriptor close cannot be
        retried because its number may have been reused; enclosing-process
        retirement is then required before external workspace cleanup.
        """

        self._retiring = True

        async with self._operation:
            if self._retired:
                return

            failures = []

            for operation in (self._retire_readers, self.uploads.retire):
                try:
                    await operation()
                except (Exception, BaseExceptionGroup) as cause:
                    failures.append(cause)

            if self._close_failure is not None:
                failures.append(self._close_failure)

            if failures:
                raise BaseExceptionGroup("Source workspace retained because retirement is unconfirmed", failures)

            if self._directory_fd is not None:
                descriptor, self._directory_fd = self._directory_fd, None

                try:
                    os.close(descriptor)
                except OSError as cause:
                    self._close_failure = UploadFailed(
                        "Source directory closure is unconfirmed; retire the enclosing process before cleanup",
                        cause=cause,
                    )
                    raise self._close_failure from None

            if self._created:
                shutil.rmtree(self.directory)

            self._retired = True
