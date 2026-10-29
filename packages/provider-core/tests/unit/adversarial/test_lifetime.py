"""Independent live supervisors contend for the same lifetime lock."""

import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from provider_core.lifetime import LifetimeRefused, ProviderLifetime


class ProviderLifetimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def contender(self, mode):
        result = subprocess.run([
            sys.executable, "-I", "-B", str(Path(__file__).parents[2] / "fakes/lifetime_contender.py"),
            mode, str(self.root),
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    async def test_live_lock_excludes_another_owner_without_creating_database(self):
        lifetime = ProviderLifetime(self.root).open()
        self.addCleanup(lifetime.close)
        inode = (self.root / ".checkpoint-owner.lock").stat().st_ino
        self.assertEqual("blocked", self.contender("live"))
        self.assertFalse((self.root / "checkpoints.sqlite3").exists())
        with self.assertRaises(LifetimeRefused):
            lifetime.open()
        self.assertEqual("blocked", self.contender("live"))
        lifetime.close()
        self.assertEqual("acquired", self.contender("live"))
        self.assertEqual(inode, (self.root / ".checkpoint-owner.lock").stat().st_ino)

    async def test_process_death_releases_the_same_lock_inode(self):
        child = subprocess.Popen([
            sys.executable, "-I", "-B", str(Path(__file__).parents[2] / "fakes/lifetime_contender.py"),
            "hold", str(self.root),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], "Lifetime child did not report readiness")
            self.assertEqual("acquired", child.stdout.readline().strip())
            inode = (self.root / ".checkpoint-owner.lock").stat().st_ino
            self.assertEqual("blocked", self.contender("live"))
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertEqual("acquired", self.contender("live"))
        self.assertEqual(inode, (self.root / ".checkpoint-owner.lock").stat().st_ino)

    async def test_nonprivate_root_or_lock_is_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(LifetimeRefused):
            ProviderLifetime(self.root).open()
        self.assertEqual([], list(self.root.iterdir()))
        self.root.chmod(0o700)
        lock = self.root / ".checkpoint-owner.lock"
        lock.touch(mode=0o644)
        with self.assertRaises(LifetimeRefused):
            ProviderLifetime(self.root).open()

    async def test_failed_close_does_not_retry_a_reused_descriptor_or_allow_reopening(self):
        lifetime = ProviderLifetime(self.root).open()
        close_failure = OSError(errno.EIO, "fixture close reported failure after releasing the descriptor")
        real_close = os.close
        reused = None

        def close_then_reuse(descriptor):
            nonlocal reused
            real_close(descriptor)
            reused = os.open(self.root / "unrelated", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            self.assertEqual(descriptor, reused)
            raise close_failure

        try:
            with patch("provider_core.lifetime.os.close", side_effect=close_then_reuse):
                with self.assertRaises(OSError) as first:
                    lifetime.close()
            self.assertIs(close_failure, first.exception)
            with patch("provider_core.lifetime.os.close", wraps=real_close) as close:
                with self.assertRaises(OSError) as repeated:
                    lifetime.close()
                self.assertIs(close_failure, repeated.exception)
                close.assert_not_called()
            os.fstat(reused)
            with self.assertRaises(LifetimeRefused) as reopened:
                lifetime.open()
            self.assertIs(close_failure, reopened.exception.__cause__)
            os.fstat(reused)
        finally:
            if reused is not None:
                try:
                    real_close(reused)
                except OSError as failure:
                    if failure.errno != errno.EBADF:
                        raise

    async def test_admission_and_close_failures_are_preserved_independently(self):
        lifetime = ProviderLifetime(self.root)
        admission_failure = BlockingIOError(errno.EAGAIN, "fixture lock admission refused")
        close_failure = OSError(errno.EIO, "fixture lock close reported failure")
        real_close = os.close

        def close_then_fail(descriptor):
            real_close(descriptor)
            raise close_failure

        with (patch("provider_core.lifetime.fcntl.flock", side_effect=admission_failure),
              patch("provider_core.lifetime.os.close", side_effect=close_then_fail)):
            with self.assertRaises(BaseExceptionGroup) as failed:
                lifetime.open()
        self.assertEqual((admission_failure, close_failure), failed.exception.exceptions)
        with self.assertRaises(LifetimeRefused) as reopened:
            lifetime.open()
        self.assertIs(close_failure, reopened.exception.__cause__)
