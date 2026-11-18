"""The released bundle uses the declared dependency bytes and source."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
import pathlib

from build_bundle import build
from sdk_build.core_environment import wheelhouse_source_inventory
from tools.installer import create_environment, pip_command
from tests.repository.quality.sources import ROOT


class WheelhouseBundleTests(unittest.TestCase):
    def test_target_pip_does_not_expose_other_checker_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            canary = root / "checker-site/checker_only_canary-1.0.dist-info"

            canary.mkdir(parents=True)
            (canary / "METADATA").write_text("Name: checker-only-canary\nVersion: 1.0\n")
            environment = root / "target"
            command = pip_command(create_environment(environment))

            completed_process = subprocess.run(
                [*command, "list", "--format=json"],
                env=os.environ | {"PYTHONPATH": str(root / "checker-site")},
                check=True,
                capture_output=True,
                text=True
            )

            self.assertNotIn("checker-only-canary", {item["name"] for item in json.loads(completed_process.stdout)})

    def test_bundle_rebuild_is_stable_and_refuses_wrong_dependency_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)

            (root / "wheelhouse_tool").mkdir()

            for name in ("__init__.py", "__main__.py", "stream.py", "report.py", "identity.py", "locate.py", "network.py"):
                (root / "wheelhouse_tool" / name).write_text(f"# source: {name}\n")

            wheel = root / "example-1.0-py3-none-any.whl"

            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("example/__init__.py", "# dependency\n")

            lock = root / "tool.lock"

            lock.write_text(f"example==1.0 \\\n    --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
            (first, second) = (root / "first.pyz", root / "second.pyz")
            type_requirements_source = ROOT / "packages/type-requirements/src/type_requirements.py"

            build(root, first, root, lock, type_requirements_source=type_requirements_source)
            build(root, second, root, lock, type_requirements_source=type_requirements_source)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            inventory = wheelhouse_source_inventory(
                root / "wheelhouse_tool",
                type_requirements_source=type_requirements_source,
            )

            with zipfile.ZipFile(first) as archive:
                payload = {name.removeprefix("wheelhouse_tool/"): archive.read(name)
                           for name in archive.namelist() if name.startswith("wheelhouse_tool/")}
                payload["type_requirements.py"] = archive.read("type_requirements.py")

            self.assertEqual(set(inventory), set(payload))
            self.assertEqual(type_requirements_source.read_bytes(), payload["type_requirements.py"])

            subprocess.run(
                [
                    str(create_environment(root / "consumer")),
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from type_requirements import require_type; require_type(1, int)",
                    str(first),
                ],
                check=True,
            )

            for (name, data) in payload.items():
                self.assertEqual(inventory[name]["sha256"], hashlib.sha256(data).hexdigest())

            wheel.write_bytes(b"changed")

            with self.assertRaisesRegex(ValueError, "pinned hash"):
                build(root, root / "wrong.pyz", root, lock, type_requirements_source=type_requirements_source)

            self.assertFalse((root / "wrong.pyz").exists())
