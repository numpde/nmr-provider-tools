"""Complete operations preserve failures and withhold mappings until retirement."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from provider_deploy.model import Bounds, ProbeFailure, ProbeRequest
from provider_deploy.operation import probe_identity
from provider_deploy.docker import DockerError, PROBE_SCRIPT


CID = "c" * 64
IMAGE = "sha256:" + "a" * 64


@dataclass(frozen=True)
class Identity:
    context: str = "selected"
    endpoint: str = "unix:///selected.sock"
    daemon_id: str = "selected-daemon"


@dataclass(frozen=True)
class Map:
    offset: int

    def translate(self, number):
        return self.offset + number


@dataclass(frozen=True)
class Observation:
    pid: int = 42
    starttime: int = 123
    proc_inode: int = 999
    uid_map: Map = Map(100000)
    gid_map: Map = Map(200000)


@dataclass(frozen=True)
class Host:
    user_namespace_inode: int = 4026531837
    pid_namespace_inode: int = 4026531836


class Engine:
    identity = Identity()

    def __init__(self):
        self.events = []
        self.container = None
        self.create_failure = None
        self.hide_creation = False
        self.remove_failure = False
        self.mutate_container = None
        self.cancel_on_create = None
        self.after_start = None

    def image_id(self, image, **kwargs):
        self.events.append("image")
        return IMAGE

    def create(self, **kwargs):
        self.events.append("create")
        self.create_kwargs = kwargs
        # Recovery identity is durably present BEFORE dispatch.
        journal = self.journal.read_text()
        assert kwargs["token"] in journal and "prepared" in journal
        if not self.hide_creation:
            self.container = SimpleNamespace(id=CID, name=kwargs["name"], token=kwargs["token"],
                owner="identity-v1", image_id=IMAGE, user=f'{kwargs["uid"]}:{kwargs["gid"]}',
                userns_mode="" if kwargs["userns_mode"] == "default" else "host",
                entrypoint=("/bin/sh",), command=("-c", PROBE_SCRIPT, kwargs["token"], str(kwargs["lifetime_seconds"])),
                running=False, pid=0, status="created", started_at="not-started")
        if self.mutate_container:
            self.mutate_container(self.container)
        if self.cancel_on_create:
            self.cancel_on_create.set()
        if self.create_failure:
            raise self.create_failure
        return CID

    def start(self, cid, **kwargs):
        self.events.append("start")
        self.container.running = True
        self.container.pid = 42
        self.container.status = "running"
        self.container.started_at = "2026-10-31T00:00:00Z"
        if self.after_start:
            self.after_start(self.container)

    def inspect(self, cid, **kwargs):
        self.events.append("inspect")
        return self.container

    def candidates(self, token, **kwargs):
        self.events.append("candidates")
        return (CID,) if self.container else ()

    def remove(self, cid, **kwargs):
        self.events.append("remove")
        assert kwargs.get("cancel") is None, "cleanup must ignore cancellation"
        if self.remove_failure:
            raise DockerError("docker_command", "safe error", mutation_uncertain=True)
        self.container = None

    def assert_unchanged(self, **kwargs):
        self.events.append("engine-check")

    def close(self):
        self.events.append("close")


class Guard:
    def __init__(self):
        self.observations = [Observation(), Observation()]
        self.retired = True

    def host_evidence(self):
        return Host()

    def observe(self, pid, token):
        return self.observations.pop(0)

    def process_retired(self, observation):
        return self.retired


class OperationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "operation.jsonl"
        self.request = ProbeRequest("selected", "approved:local", 1000, 2000, "default",
                                    (0, 1000), (0, 2000), self.path, Bounds(10, 1, 2, 30))
        self.engine = Engine()
        self.engine.journal = self.path
        self.guard = Guard()
        p = patch("provider_deploy.operation.DockerEngine.select", return_value=self.engine)
        p.start()
        self.addCleanup(p.stop)
        p = patch("provider_deploy.operation.LinuxGuard", return_value=self.guard)
        p.start()
        self.addCleanup(p.stop)

    def failure(self, cancel=None):
        with self.assertRaises(ProbeFailure) as caught:
            probe_identity(self.request, cancel=cancel)
        self.assertNotIn("mapped_uids", caught.exception.as_dict())
        self.assertNotIn("mapped_gids", caught.exception.as_dict())
        return caught.exception

    def test_success_is_released_only_after_exact_removal_and_absence(self):
        result = probe_identity(self.request)
        self.assertEqual(((0, 100000), (1000, 101000)), result.mapped_uids)
        self.assertEqual(((0, 200000), (2000, 202000)), result.mapped_gids)
        self.assertTrue(result.retirement_confirmed)
        self.assertIsNone(self.engine.container)
        self.assertIn("remove", self.engine.events)
        self.assertLess(self.engine.events.index("remove"), len(self.engine.events) - 1)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual("confirmed-retired", json.loads(self.path.read_text().splitlines()[-1])["state"])

    def test_lost_creation_output_reconciles_and_preserves_original_failure(self):
        self.engine.create_failure = DockerError("timeout", "safe error", mutation_uncertain=True)
        failure = self.failure()
        self.assertEqual("timeout", failure.category)
        self.assertEqual("confirmed-retired", failure.state)
        self.assertIn("candidates", self.engine.events)
        self.assertEqual(1, self.engine.events.count("create"))
        self.assertIn("remove", self.engine.events)

    def test_lost_creation_output_and_empty_inventory_remains_uncertain(self):
        self.engine.create_failure = DockerError("timeout", "safe error", mutation_uncertain=True)
        self.engine.hide_creation = True
        failure = self.failure()
        self.assertEqual("uncertain-outcome", failure.state)
        self.assertNotIn("remove", self.engine.events)

    def test_foreign_token_is_never_removed(self):
        self.engine.mutate_container = lambda c: setattr(c, "token", "foreign")
        failure = self.failure()
        self.assertEqual("ownership", failure.category)
        self.assertNotIn("remove", self.engine.events)
        self.assertTrue(failure.cleanup_failures)

    def test_cleanup_failure_does_not_release_successful_mapping(self):
        self.engine.remove_failure = True
        failure = self.failure()
        self.assertEqual("retirement", failure.category)
        self.assertTrue(failure.cleanup_failures)

    def test_cancellation_after_create_still_enters_cleanup(self):
        event = threading.Event()
        self.engine.cancel_on_create = event
        failure = self.failure(event)
        self.assertEqual("cancelled", failure.category)
        self.assertEqual("confirmed-retired", failure.state)
        self.assertIn("remove", self.engine.events)
        self.assertNotIn("start", self.engine.events)

    def test_cancellation_before_dispatch_is_known_not_created(self):
        event = threading.Event()
        event.set()
        failure = self.failure(event)
        self.assertEqual("known-not-created", failure.state)
        self.assertNotIn("create", self.engine.events)

    def test_pid_reuse_is_not_a_success(self):
        self.guard.observations[1] = replace(Observation(), starttime=124)
        failure = self.failure()
        self.assertEqual("process-identity", failure.category)
        self.assertEqual("confirmed-retired", failure.state)

    def test_map_changes_are_not_a_success(self):
        self.guard.observations[1] = replace(Observation(), gid_map=Map(300000))
        failure = self.failure()
        self.assertEqual("process-identity", failure.category)

    def test_exited_probe_is_removed_without_success(self):
        self.engine.after_start = lambda c: setattr(c, "running", False)
        failure = self.failure()
        self.assertEqual("process-identity", failure.category)
        self.assertIn("remove", self.engine.events)

    def test_missing_container_with_still_live_process_is_uncertain(self):
        self.guard.retired = False
        failure = self.failure()
        self.assertEqual("uncertain-outcome", failure.state)
        self.assertIn("process-retirement", failure.cleanup_failures)

    def test_existing_recovery_file_is_not_overwritten(self):
        self.path.write_text("prior evidence")
        failure = self.failure()
        self.assertEqual("journal", failure.category)
        self.assertEqual("prior evidence", self.path.read_text())
        self.assertNotIn("create", self.engine.events)

    def test_original_error_and_cleanup_error_are_both_retained(self):
        self.engine.create_failure = DockerError("timeout", "SECRET ENGINE OUTPUT", mutation_uncertain=True)
        self.engine.remove_failure = True
        failure = self.failure()
        self.assertEqual("timeout", failure.category)
        self.assertTrue(failure.cleanup_failures)
        self.assertNotIn("SECRET", json.dumps(failure.as_dict()))


if __name__ == "__main__":
    unittest.main()
