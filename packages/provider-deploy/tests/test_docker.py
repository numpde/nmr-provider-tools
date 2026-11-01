"""Docker command boundaries; no daemon or network is needed for this lane."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from dataclasses import asdict

from provider_deploy.spec import ProbeSpec, PROBE_SCRIPT, OWNER
from helpers import container_for
import unittest
from unittest.mock import patch

from provider_deploy.docker import (
    DockerEngine, DockerError, OWNER_LABEL, TOKEN_LABEL,
    _run_command, _clean_environment, _socket_identity,
)


CID = "c" * 64
IMAGE = "sha256:" + "a" * 64
TOKEN = "b" * 32


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
                                  "DOCKER_API_VERSION": "1", "HTTP_PROXY": "SECRET", "COMPOSE_FILE": "bad"})
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


class EndpointContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.first, self.second = root / "first.sock", root / "second.sock"
        self.selected = root / "selected.sock"
        self.selected.symlink_to(self.first)
        self.endpoint = "unix://" + str(self.selected)
        # Real symlink traversal, modeled socket identities: no AF_UNIX daemon
        # is available in this lane. Both targets report the same daemon info
        # so only continuity of the caller's socket path can detect the change.
        identities = {self.first: (12, 34), self.second: (12, 35)}
        identity_patch = patch("provider_deploy.docker._socket_identity",
                               side_effect=lambda path: identities[Path(path).resolve()])
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        self.calls = []
        def run(argv, **kwargs):
            self.calls.append(argv)
            return json.dumps(["daemon-a", [], "/var/lib/docker", "overlay2", "linux"])
        self.run = run
        runner = patch("provider_deploy.docker._run_command", side_effect=run)
        runner.start()
        self.addCleanup(runner.stop)

    def retarget(self):
        self.selected.unlink()
        self.selected.symlink_to(self.second)

    def test_symlink_endpoint_spelling_is_preserved_for_identity_and_commands(self):
        with DockerEngine.connect(self.endpoint, timeout=2) as engine:
            self.assertEqual(self.endpoint, engine.identity.endpoint)
            engine.assert_unchanged(timeout=2)
        for argv in self.calls:
            self.assertEqual(self.endpoint, argv[argv.index("--host") + 1])

    def test_symlink_retarget_during_connect_is_rejected(self):
        def retarget_after_info(argv, **kwargs):
            result = self.run(argv, **kwargs)
            self.retarget()
            return result
        with patch("provider_deploy.docker._run_command", side_effect=retarget_after_info):
            with self.assertRaises(DockerError) as caught:
                with DockerEngine.connect(self.endpoint, timeout=2):
                    pass
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertEqual(1, len(self.calls))

    def test_symlink_retarget_after_connect_stops_before_further_mutation(self):
        with DockerEngine.connect(self.endpoint, timeout=2) as engine:
            self.retarget()
            self.calls.clear()
            with self.assertRaises(DockerError) as caught:
                engine.start(CID, timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertFalse(caught.exception.mutation_uncertain)
        self.assertEqual([], self.calls)


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
        self.security_options = ["name=rootless"]
        self.extra = {}
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "context" in argv:
                return json.dumps("unix://" + str(self.sockpath))
            if "info" in argv:
                return json.dumps([self.daemon, self.security_options, "/var/lib/docker", "overlay2", "linux"])
            key = next((x for x in ("create", "inspect", "ls", "start", "rm") if x in argv), "other")
            # silent-defaults: allow-contract -- An unconfigured successful fixture command emits empty output; parser tests explicitly replace its reply.
            result = self.extra.get(key, "")
            if isinstance(result, BaseException):
                raise result
            return result
        self.record_run = run
        self.runner = patch("provider_deploy.docker._run_command", side_effect=run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.engine = DockerEngine.connect("unix://" + str(self.sockpath), timeout=2)
        self.addCleanup(self.engine.close)

    def test_caller_endpoint_is_never_reresolved_and_ambient_settings_are_ignored(self):
        self.extra["inspect"] = json.dumps([IMAGE, None])
        self.assertEqual(IMAGE, self.engine.image_id("approved:local", timeout=2))
        self.assertFalse(any("context" in args for args, _ in self.calls))
        for args, kwargs in self.calls:
            self.assertEqual("unix://" + str(self.sockpath), args[args.index("--host") + 1])
            self.assertIn("--config", args)
            self.assertNotIn("DOCKER_HOST", kwargs["env"])

    def test_image_projection_uses_typed_id_and_accepts_empty_volumes(self):
        # Source compatibility, not engine qualification: .Id falls back to a
        # raw map, whose omitted Volumes key fails missingkey=error. Typed .ID
        # keeps the nil map available for json to encode as null.
        # https://github.com/docker/cli/blob/v28.5.1/cli/command/inspect/inspector.go#L100-L129
        # https://github.com/moby/moby/blob/v28.5.1/api/types/image/image_inspect.go#L16-L83
        # https://github.com/opencontainers/image-spec/blob/v1.1.1/specs-go/v1/config.go#L36-L37
        for volumes in (None, {}):
            with self.subTest(volumes=volumes):
                self.extra["inspect"] = json.dumps([IMAGE, volumes])
                self.assertEqual(IMAGE, self.engine.image_id("approved:local", timeout=2))
                args = next(a for a, _ in reversed(self.calls) if "inspect" in a)
                self.assertEqual("[{{json .ID}},{{json .Config.Volumes}}]", args[args.index("--format") + 1])

    def test_null_and_empty_security_options_are_equivalent_but_change_is_not(self):
        # A nil security-options slice is legitimate when no features append.
        # https://github.com/moby/moby/blob/v28.5.1/daemon/info.go#L166-L192
        # https://github.com/docker/cli/blob/v28.5.1/cli/command/system/info.go#L450-L468
        for initial, equivalent in ((None, []), ([], None)):
            with self.subTest(initial=initial):
                self.security_options = initial
                with DockerEngine.connect("unix://" + str(self.sockpath), timeout=2) as engine:
                    self.assertEqual((), engine.identity.security_options)
                    self.security_options = equivalent
                    engine.assert_unchanged(timeout=2)
                    self.security_options = ["name=userns"]
                    self.calls.clear()
                    with self.assertRaises(DockerError) as caught:
                        engine.start(CID, timeout=2)
                    self.assertEqual("engine_changed", caught.exception.category)
                    self.assertFalse(any("start" in args for args, _ in self.calls))

    def test_malformed_security_options_remain_rejected(self):
        for options in (False, 0, "", "name=rootless", {}, [None], [False], [1], [[]], [{}]):
            with self.subTest(options=options):
                self.security_options = options
                with self.assertRaises(DockerError) as caught:
                    DockerEngine.connect("unix://" + str(self.sockpath), timeout=2)
                self.assertEqual("invalid_response", caught.exception.category)

    def test_missing_image_does_not_pull(self):
        self.extra["inspect"] = '"not-an-id"'
        with self.assertRaises(DockerError):
            self.engine.image_id("missing:local", timeout=2)
        self.assertFalse(any("pull" in args for args, _ in self.calls))

    def test_create_is_owned_restricted_and_never_pulls(self):
        self.extra["create"] = CID + "\n"
        result = self.engine.create(ProbeSpec(TOKEN, IMAGE, "/usr/bin/python3", 1000, 2000, "host", 30), timeout=2)
        self.assertEqual(CID, result)
        args, kwargs = next((a, k) for a, k in self.calls if "create" in a)
        self.assertTrue(kwargs["mutation"])
        self.assertIn("--pull=never", args)
        self.assertIn(TOKEN_LABEL + "=" + TOKEN, args)
        self.assertIn(OWNER_LABEL + "=" + OWNER, args)
        self.assertIn("--userns=host", args)
        self.assertIn("1000:2000", args)
        self.assertEqual(["-I", "-S", "-B", "-c", PROBE_SCRIPT, "30", TOKEN], args[-7:])
        self.assertIn("--rm", args)
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
        self.extra["inspect"] = DockerError("docker_command", "Docker command did not report success.")
        self.extra["ls"] = ""
        self.assertIsNone(self.engine.inspect(CID, timeout=2))
        args = next(a for a, _ in self.calls if "ls" in a)
        self.assertIn("--all", args)
        self.assertIn("--no-trunc", args)
        self.assertIn("id=" + CID, args)
        self.assertTrue(any("container" in args and "inspect" in args for args, _ in self.calls))

    def test_inspect_failure_is_not_absence_while_exact_id_is_listed(self):
        failure = DockerError("docker_command", "Docker command did not report success.")
        self.extra.update(inspect=failure, ls=CID + "\n")
        with self.assertRaises(DockerError) as caught:
            self.engine.inspect(CID, timeout=2)
        self.assertIs(failure, caught.exception)

    def test_failed_listing_cannot_turn_inspect_failure_into_absence(self):
        self.extra.update(inspect=DockerError("docker_command", "inspect failed"),
                          ls=DockerError("docker_command", "listing failed"))
        with self.assertRaises(DockerError):
            self.engine.inspect(CID, timeout=2)

    def test_inspect_timeout_does_not_start_an_absence_query(self):
        self.extra["inspect"] = DockerError("timeout", "inspection timed out")
        with self.assertRaises(DockerError) as caught:
            self.engine.inspect(CID, timeout=2)
        self.assertEqual("timeout", caught.exception.category)
        self.assertFalse(any("ls" in args for args, _ in self.calls))

    def test_candidates_reject_partial_ids(self):
        self.extra["ls"] = CID[:12] + "\n"
        with self.assertRaises(DockerError):
            self.engine.candidates(TOKEN, timeout=2)

    def test_remote_endpoint_rejected_before_engine_command(self):
        with patch("provider_deploy.docker._run_command") as run:
            with self.assertRaises(DockerError) as caught:
                DockerEngine.connect("ssh://remote", timeout=2)
        self.assertEqual("unsupported_engine", caught.exception.category)
        self.assertEqual(0, run.call_count)

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
            self.engine.create(ProbeSpec(TOKEN, IMAGE, "/usr/bin/python3", 0, 0, "default", 30), timeout=2)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertEqual(1, sum("create" in args for args, _ in self.calls))

    def test_inspect_only_returns_curated_exact_evidence(self):
        self.extra["ls"] = CID + "\n"
        evidence = asdict(container_for(ProbeSpec(TOKEN, IMAGE, "/usr/bin/python3", 1000, 2000, "default", 30)))
        evidence["name"] = "/provider-deploy-" + TOKEN
        self.extra["inspect"] = json.dumps(evidence)
        result = self.engine.inspect(CID, timeout=2)
        self.assertEqual(CID, result.cid)
        self.assertEqual("provider-deploy-" + TOKEN, result.name)
        self.assertEqual(("-I", "-S", "-B", "-c", PROBE_SCRIPT, "30", TOKEN), result.command)
        self.assertFalse(any("ls" in args for args, _ in self.calls))
        # The connection checks once; one existing-container read then costs
        # info / inspect / info, not a second list plus its two info checks.
        self.assertEqual(4, len(self.calls))

    def test_inspect_substituted_container_is_rejected(self):
        evidence = asdict(container_for(ProbeSpec(TOKEN, IMAGE, "/usr/bin/python3", 1000, 2000, "default", 30)))
        evidence["cid"] = "d" * 64
        self.extra["inspect"] = json.dumps(evidence)
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
                self.engine.create(ProbeSpec(TOKEN, IMAGE, "/usr/bin/python3", 0, 0, "default", 30), timeout=2)
        self.assertEqual("engine_changed", caught.exception.category)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertEqual(1, sum("create" in args for args, _ in self.calls))

    def test_ambient_context_and_config_cannot_change_pinned_endpoint(self):
        with patch.dict(os.environ, {"DOCKER_CONFIG": "/custom/store", "DOCKER_HOST": "tcp://ambient", "DOCKER_CONTEXT": "other"}):
            with DockerEngine.connect("unix://" + str(self.sockpath), timeout=2):
                pass
        args, kwargs = self.calls[-1]
        self.assertNotIn("DOCKER_CONFIG", kwargs["env"])
        self.assertNotIn("DOCKER_HOST", kwargs["env"])
        self.assertNotIn("context", args)


if __name__ == "__main__":
    unittest.main()
