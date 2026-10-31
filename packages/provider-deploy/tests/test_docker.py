"""Docker command boundaries; no daemon or network is needed for this lane."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from provider_deploy.docker import (
    DockerEngine, DockerError, OWNER_LABEL, TOKEN_LABEL, PROBE_SCRIPT,
    _run_command, _clean_environment,
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


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sockpath = Path(self.tmp.name) / "engine.sock"
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.sockpath))
        self.addCleanup(self.sock.close)
        self.calls = []
        self.daemon = "daemon-a"
        self.extra = {}
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "context" in argv:
                return json.dumps("unix://" + str(self.sockpath))
            if "info" in argv:
                return json.dumps(self.daemon)
            key = next((x for x in ("create", "inspect", "ls", "start", "rm") if x in argv), "other")
            return self.extra.get(key, "")
        self.runner = patch("provider_deploy.docker._run_command", side_effect=run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.engine = DockerEngine.select("explicit", timeout=2)
        self.addCleanup(self.engine.close)

    def test_context_resolved_once_all_engine_calls_pin_endpoint(self):
        self.extra["inspect"] = json.dumps(IMAGE)
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
        self.sockpath.unlink()
        replacement = socket.socket(socket.AF_UNIX)
        replacement.bind(str(self.sockpath))
        self.addCleanup(replacement.close)
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
        self.assertFalse(any("inspect" in args for args, _ in self.calls))

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


if __name__ == "__main__":
    unittest.main()
