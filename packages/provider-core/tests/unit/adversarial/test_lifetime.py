"""The live supervisor and existing checkpoint owner exclude each other."""

from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.lifetime import LifetimeRefused, ProviderLifetime
from tests.fakes.attempt_store import scope


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

    async def test_live_lock_excludes_checkpoint_owner_without_creating_database(self):
        lifetime = ProviderLifetime(self.root).open()
        self.addCleanup(lifetime.close)
        inode = (self.root / ".checkpoint-owner.lock").stat().st_ino
        self.assertEqual("blocked", self.contender("checkpoint"))
        self.assertFalse((self.root / "checkpoints.sqlite3").exists())
        with self.assertRaises(LifetimeRefused):
            lifetime.open()
        self.assertEqual("blocked", self.contender("live"))
        lifetime.close()
        self.assertEqual("acquired", self.contender("checkpoint"))
        self.assertEqual(inode, (self.root / ".checkpoint-owner.lock").stat().st_ino)

    async def test_existing_checkpoint_lock_excludes_live_supervisor(self):
        store = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
        await store.open()
        try:
            self.assertEqual("blocked", self.contender("live"))
        finally:
            await store.close()
        self.assertEqual("acquired", self.contender("live"))

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
