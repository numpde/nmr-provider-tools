"""Native GC JSON owns both deferred daemon policy and periodic prune limits."""
from tools.container_guard import require_container
require_container()

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host.tools.engine_cache_gc import configured, install, limits, unit_files
from tests.repository.quality.sources import ROOT


class EngineCacheGCTests(unittest.TestCase):
    def test_native_policy_merge_preserves_unrelated_fields_and_runtime_reads_it(self):
        original = {"data-root": "/owned/engine", "log-driver": "local",
                    "builder": {"other": {"retained": True}}}
        updated = configured(original)
        self.assertEqual(original["data-root"], updated["data-root"])
        self.assertEqual(original["builder"]["other"], updated["builder"]["other"])
        self.assertNotIn("gc", original["builder"])
        self.assertEqual(["10GB", "32GB", "20GB"], limits(updated))
        updated["builder"]["gc"]["defaultMaxUsedSpace"] = "24GB"
        self.assertEqual(["10GB", "24GB", "20GB"], limits(updated))
        updated["builder"]["gc"]["enabled"] = False
        self.assertIsNone(limits(updated))

    def test_timer_identity_is_per_endpoint_and_arguments_remain_literal(self):
        one = unit_files(Path('/checkout with space%/'), "science", "unix:///tmp/science.sock", Path('/config/$literal/daemon.json'))
        other_context = unit_files(Path('/other/checkout'), "alias", "unix:///tmp/science.sock", Path('/other/daemon.json'))
        other_engine = unit_files(Path('/checkout'), "other", "unix:///tmp/other.sock", Path('/config/daemon.json'))
        self.assertEqual(one[0], other_context[0])
        self.assertNotEqual(one[0], other_engine[0])
        self.assertIn('ExecStart=:', one[1])
        self.assertIn('space%%', one[1])
        self.assertIn('$literal', one[1])
        self.assertIn('OnCalendar=hourly', one[2])

    def test_invalid_dockerd_candidate_never_replaces_native_json_or_installs_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "daemon.json"
            config.write_text('{"data-root":"/keep"}\n')
            before = config.read_bytes()
            with patch("host.tools.engine_cache_gc.subprocess.run", return_value=SimpleNamespace(returncode=1)) as command:
                with self.assertRaisesRegex(ValueError, "original file is unchanged"):
                    install(root, "science", "unix:///tmp/engine.sock", config)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(["dockerd", "--validate"], command.call_args.args[0][:2])
            self.assertEqual([config], list(root.iterdir()))

    def test_timer_failure_reports_saved_native_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "daemon.json"
            config.write_text("{}\n")
            failure = subprocess.CalledProcessError(1, ["systemctl", "--user", "daemon-reload"])
            with patch(
                "host.tools.engine_cache_gc.subprocess.run",
                side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(stdout=str(root / "config") + "\n"), failure],
            ):
                with self.assertRaisesRegex(RuntimeError, "settings were saved.*activation could not be confirmed"):
                    install(root, "science", "unix:///tmp/engine.sock", config)
            self.assertEqual(["10GB", "32GB", "20GB"], limits(json.loads(config.read_text())))

    def test_periodic_command_uses_native_limits_and_isolated_selected_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            for name in ("engine_cache_gc.sh", "engine_cache_gc.py", "docker_context.sh", "docker_client.sh", "inference_native.sh"):
                shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
            config = root / "daemon.json"
            document = configured({})
            document["builder"]["gc"]["defaultMaxUsedSpace"] = "24GB"
            config.write_text(json.dumps(document))
            probe = root / "probe.json"
            result = subprocess.run(["bash", str(root / "host/tools/engine_cache_gc.sh"), "run", "science",
                                     "unix:///tmp/selected-engine.sock", str(config)], capture_output=True, text=True,
                                    env=os.environ | {"PATH":str(ROOT / "tests/repository/fixtures/cache-gc") + os.pathsep + os.environ["PATH"],
                                                      "GC_PROBE":str(probe), "DOCKER_HOST":"unix:///tmp/wrong.sock",
                                                      "HTTP_PROXY":"http://unselected.invalid"}, timeout=15, check=False)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            observed = json.loads(probe.read_text())
            argv = observed["argv"]
            self.assertEqual("unix:///tmp/selected-engine.sock", argv[argv.index("--host") + 1])
            self.assertEqual("default", argv[argv.index("--builder") + 1])
            self.assertEqual("10GB", argv[argv.index("--reserved-space") + 1])
            self.assertEqual("24GB", argv[argv.index("--max-used-space") + 1])
            self.assertEqual("20GB", argv[argv.index("--min-free-space") + 1])
            self.assertNotIn("DOCKER_HOST", observed["env"])
            self.assertNotIn("HTTP_PROXY", observed["env"])
            self.assertFalse(Path(argv[argv.index("--config") + 1]).exists())
