"""Docker command boundaries; no daemon or network is needed for this lane."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from provider_deploy.docker import (
    DockerEngine, DockerError, OWNER_LABEL, TOKEN_LABEL, PROBE_SCRIPT,
    _run_command, _clean_environment, _socket_identity,
)


CID = "c" * 64
IMAGE = "sha256:" + "a" * 64
TOKEN = "b" * 64


class CommandTests(unittest.TestCase):
    def test_timeout_kills_real_child_and_bounds_wait(self):
        started = time.monotonic()
        with self.assertRaises(DockerError) as caught:
            _run_command([sys.executable, "-c", "import time; time.sleep(20)"],
                         env=os.environ.copy(), timeout=0.05, mutation=True)
        self.assertEqual("timeout", caught.exception.category)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertLess(time.monotonic() - started, 2)

    def test_output_is_bounded_without_echoing_content(self):
        with self.assertRaises(DockerError) as caught:
            _run_command([sys.executable, "-c", "print('SECRET'*20000)"],
                         env=os.environ.copy(), timeout=2)
        self.assertEqual("output_limit", caught.exception.category)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_cancel_before_dispatch_is_not_uncertain(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DockerError) as caught:
            _run_command([sys.executable, "-c", "raise AssertionError"],
                         env=os.environ.copy(), timeout=1, cancel=cancel, mutation=True)
        self.assertEqual("cancelled", caught.exception.category)
        self.assertFalse(caught.exception.mutation_uncertain)

    def test_cancel_after_dispatch_is_uncertain(self):
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            with self.assertRaises(DockerError) as caught:
                _run_command([sys.executable, "-c", "import time; time.sleep(20)"],
                             env=os.environ.copy(), timeout=2, cancel=cancel, mutation=True)
        finally:
            timer.join()
        self.assertEqual("cancelled", caught.exception.category)
        self.assertTrue(caught.exception.mutation_uncertain)

    def test_nonzero_output_never_escapes(self):
        with self.assertRaises(DockerError) as caught:
            _run_command([sys.executable, "-c", "import sys;sys.stderr.write('SECRET');sys.exit(9)"],
                         env=os.environ.copy(), timeout=2, mutation=True)
        self.assertEqual("docker_command", caught.exception.category)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_environment_strips_engine_and_injection_settings(self):
        env = _clean_environment({"PATH": "/bin", "DOCKER_HOST": "tcp://bad", "DOCKER_CONFIG": "/bad",
                                  "DOCKER_API_VERSION": "1", "HTTP_PROXY": "SECRET", "COMPOSE_FILE": "bad"},
                                 context_lookup=False)
        self.assertEqual({"PATH": "/bin"}, env)

    def test_cancel_remains_responsive_after_child_closes_pipes(self):
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(DockerError) as caught:
                _run_command([sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(20)"],
                             env=os.environ.copy(), timeout=5, cancel=cancel, mutation=True)
        finally:
            timer.join()
        self.assertEqual("cancelled", caught.exception.category)
        self.assertLess(time.monotonic() - started, 1)

    def test_timed_out_child_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            program = "import os,time,pathlib;pathlib.Path(%r).write_text(str(os.getpid()));time.sleep(20)" % str(pidfile)
            with self.assertRaises(DockerError):
                _run_command([sys.executable, "-c", program], env=os.environ.copy(), timeout=0.2)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_non_socket_endpoint_is_rejected_by_real_stat(self):
        with tempfile.NamedTemporaryFile() as file:
            with self.assertRaises(DockerError) as caught:
                _socket_identity(file.name)
        self.assertEqual("unsupported_engine", caught.exception.category)

    def test_primary_timeout_preserves_client_cleanup_failure(self):
        from provider_deploy import docker
        original = docker._terminate
        def terminate_and_report(process):
            original(process)
            raise DockerError("client_cleanup", "Docker client exit could not be confirmed.")
        with patch("provider_deploy.docker._terminate", side_effect=terminate_and_report):
            with self.assertRaises(DockerError) as caught:
                _run_command([sys.executable, "-c", "import time;time.sleep(20)"],
                             env=os.environ.copy(), timeout=0.05, mutation=True)
        self.assertEqual("timeout", caught.exception.category)
        self.assertEqual(("client_cleanup",), caught.exception.cleanup_failures)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sockpath = Path(self.tmp.name) / "engine.sock"
        self.socket_identity = (12, 34)
        socket_patch = patch("provider_deploy.docker._socket_identity", side_effect=lambda path: self.socket_identity)
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.calls = []
        self.daemon = "daemon-a"
        self.extra = {}
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "context" in argv:
                return json.dumps("unix://" + str(self.sockpath))
            if "info" in argv:
                return json.dumps([self.daemon, ["name=rootless"], "/var/lib/docker", "overlay2", "linux"])
            key = next((x for x in ("create", "inspect", "ls", "start", "rm") if x in argv), "other")
            return self.extra.get(key, "")
        self.record_run = run
        self.runner = patch("provider_deploy.docker._run_command", side_effect=run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.engine = DockerEngine.select("explicit", timeout=2)
        self.addCleanup(self.engine.close)

    def test_context_resolved_once_all_engine_calls_pin_endpoint(self):
        self.extra["inspect"] = json.dumps([IMAGE, None])
        self.assertEqual(IMAGE, self.engine.image_id("approved:local", timeout=2))
        self.assertEqual(1, sum("context" in args for args, _ in self.calls))
        for args, _ in self.calls[1:]:
            self.assertEqual("unix://" + str(self.sockpath), args[args.index("--host") + 1])
            self.assertIn("--config", args)

    def test_missing_image_does_not_pull(self):
        self.extra["inspect"] = '"not-an-id"'
        with self.assertRaises(DockerError):
            self.engine.image_id("missing:local", timeout=2)
        self.assertFalse(any("pull" in args for args, _ in self.calls))

    def test_create_is_owned_restricted_and_never_pulls(self):
        self.extra["create"] = CID + "\n"
        result = self.engine.create(token=TOKEN, name="provider-deploy-" + TOKEN,
                                    image_id=IMAGE, uid=1000, gid=2000, userns_mode="host",
                                    lifetime_seconds=30, timeout=2)
        self.assertEqual(CID, result)
        args, kwargs = next((a, k) for a, k in self.calls if "create" in a)
        self.assertTrue(kwargs["mutation"])
        self.assertIn("--pull=never", args)
        self.assertIn(TOKEN_LABEL + "=" + TOKEN, args)
        self.assertIn(OWNER_LABEL + "=identity-v1", args)
        self.assertIn("--userns=host", args)
        self.assertIn("1000:2000", args)
        self.assertEqual(["-c", PROBE_SCRIPT, TOKEN, "30"], args[-4:])
        self.assertNotIn("--rm", args)
        self.assertNotIn("--privileged", args)

    def test_changed_daemon_stops_before_mutation(self):
        self.daemon = "daemon-b"
        with self.assertRaises(DockerError) as caught:
            self.engine.start(CID, timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertFalse(any("start" in args for args, _ in self.calls))

    def test_replaced_socket_stops_before_mutation(self):
        self.socket_identity = (12, 35)
        with self.assertRaises(DockerError) as caught:
            self.engine.remove(CID, timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertFalse(any("rm" in args for args, _ in self.calls))

    def test_absence_requires_positive_successful_listing(self):
        self.extra["ls"] = ""
        self.assertIsNone(self.engine.inspect(CID, timeout=2))
        args = next(a for a, _ in self.calls if "ls" in a)
        self.assertIn("--all", args)
        self.assertIn("--no-trunc", args)
        self.assertIn("id=" + CID, args)
        self.assertFalse(any("container" in args and "inspect" in args for args, _ in self.calls))

    def test_candidates_reject_partial_ids(self):
        self.extra["ls"] = CID[:12] + "\n"
        with self.assertRaises(DockerError):
            self.engine.candidates(TOKEN, timeout=2)

    def test_remote_context_rejected_before_engine_command(self):
        with patch("provider_deploy.docker._run_command", return_value='"ssh://remote"') as run:
            with self.assertRaises(DockerError) as caught:
                DockerEngine.select("remote", timeout=2)
        self.assertEqual("unsupported_engine", caught.exception.category)
        self.assertEqual(1, run.call_count)

    def test_security_fingerprint_change_stops_before_mutation(self):
        original = self.engine.identity
        from dataclasses import replace
        self.engine.identity = replace(original, security_options=("name=userns",))
        with self.assertRaises(DockerError) as caught:
            self.engine.start(CID, timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertFalse(any("start" in args for args, _ in self.calls))

    def test_image_declared_volumes_are_rejected(self):
        self.extra["inspect"] = json.dumps([IMAGE, {"/data": {}}])
        with self.assertRaises(DockerError) as caught:
            self.engine.image_id("approved:local", timeout=2)
        self.assertEqual("invalid_policy", caught.exception.category)
        self.assertFalse(any("create" in args for args, _ in self.calls))

    def test_lost_create_output_is_explicitly_uncertain(self):
        self.extra["create"] = ""
        with self.assertRaises(DockerError) as caught:
            self.engine.create(token=TOKEN, name="provider-deploy-" + TOKEN, image_id=IMAGE,
                               uid=0, gid=0, userns_mode="default", lifetime_seconds=30, timeout=2)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertEqual(1, sum("create" in args for args, _ in self.calls))

    def test_inspect_only_returns_curated_exact_evidence(self):
        self.extra["ls"] = CID + "\n"
        evidence = dict(cid=CID, name="/provider-deploy-" + TOKEN, image_id=IMAGE, token=TOKEN,
                        owner="identity-v1", pid=123, running=True, status="running", started_at="timestamp",
                        user="1000:2000", userns_mode="", entrypoint=["/bin/sh"],
                        command=["-c", PROBE_SCRIPT, TOKEN, "30"])
        self.extra["inspect"] = json.dumps(evidence)
        result = self.engine.inspect(CID, timeout=2)
        self.assertEqual(CID, result.cid)
        self.assertEqual("provider-deploy-" + TOKEN, result.name)
        self.assertEqual(("-c", PROBE_SCRIPT, TOKEN, "30"), result.command)

    def test_inspect_substituted_container_is_rejected(self):
        self.extra["ls"] = "d" * 64 + "\n"
        with self.assertRaises(DockerError) as caught:
            self.engine.inspect(CID, timeout=2)
        self.assertEqual("identity_mismatch", caught.exception.category)

    def test_remove_uses_only_full_exact_id_and_no_volume_pruning(self):
        self.engine.remove(CID, timeout=2)
        args = next(args for args, _ in self.calls if "rm" in args)
        self.assertEqual(["container", "rm", "--force", CID], args[-4:])
        with self.assertRaises(DockerError):
            self.engine.remove("provider-deploy-" + TOKEN, timeout=2)

    def test_private_client_config_is_empty_and_removed_on_close(self):
        args = next(args for args, _ in self.calls if "info" in args)
        directory = Path(args[args.index("--config") + 1])
        self.assertEqual([], list(directory.iterdir()))
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        self.engine.close()
        self.assertFalse(directory.exists())

    def test_engine_change_after_create_is_uncertain_without_retry(self):
        self.extra["create"] = CID + "\n"
        # The existing recording callback mutates the daemon only after create.
        callback = self.record_run
        def change_on_create(argv, **kwargs):
            result = callback(argv, **kwargs)
            if "create" in argv:
                self.daemon = "replacement"
            return result
        with patch("provider_deploy.docker._run_command", side_effect=change_on_create):
            with self.assertRaises(DockerError) as caught:
                self.engine.create(token=TOKEN, name="provider-deploy-" + TOKEN, image_id=IMAGE,
                                   uid=0, gid=0, userns_mode="default", lifetime_seconds=30, timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertEqual(1, sum("create" in args for args, _ in self.calls))

    def test_named_context_uses_selected_store_not_same_name_in_default_store(self):
        custom_endpoint = "unix:///custom/engine.sock"
        default_endpoint = "unix:///default/engine.sock"
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if "context" in argv:
                endpoint = custom_endpoint if kwargs["env"].get("DOCKER_CONFIG") == "/custom/store" else default_endpoint
                return json.dumps(endpoint)
            return json.dumps(["custom-daemon", [], "/root", "overlay2", "linux"])
        with patch.dict(os.environ, {"DOCKER_CONFIG": "/custom/store", "DOCKER_HOST": "tcp://ambient", "DOCKER_CONTEXT": "other"}):
            with patch("provider_deploy.docker._run_command", side_effect=run):
                with DockerEngine.select("same-name", timeout=2) as engine:
                    self.assertEqual(custom_endpoint, engine.identity.endpoint)
        self.assertNotIn("DOCKER_HOST", calls[0][1]["env"])
        self.assertNotIn("DOCKER_CONTEXT", calls[0][1]["env"])
        self.assertNotIn("DOCKER_CONFIG", calls[1][1]["env"])
        self.assertIn("--config", calls[1][0])
        self.assertNotIn("/custom/store", calls[1][0])


if __name__ == "__main__":
    unittest.main()
