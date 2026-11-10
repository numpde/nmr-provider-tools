"""Release qualification refuses omitted source and unsafe archive membership."""

from tools.container_guard import require_container

require_container()

import ast
import io
from contextlib import redirect_stdout
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

from tools.package import artifact_names, check, source_payload, unpack_source, wheel_payload
from tools.core_environment import behavioral_install_lock, build_source_wheel, normalize_sdist
from tools.wheelhouse import digest
from tests.repository.quality.sources import ROOT


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_source_normalization_cleans_scratch_and_preserves_failed_input(self):
        for kind in (tarfile.REGTYPE, tarfile.SYMTYPE):
            with self.subTest(kind=kind):
                path = self.root / "source.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("package/member")
                    member.type = kind
                    member.uid = 123
                    archive.addfile(member, io.BytesIO())
                original = path.read_bytes()
                if kind == tarfile.SYMTYPE:
                    with self.assertRaisesRegex(ValueError, "unsupported link"):
                        normalize_sdist(path)
                    self.assertEqual(original, path.read_bytes())
                else:
                    normalize_sdist(path)
                    with tarfile.open(path, "r:gz") as archive:
                        self.assertEqual(0, archive.getmembers()[0].uid)
                self.assertEqual([path], list(self.root.iterdir()))

    def test_source_archive_refuses_traversal_links_duplicate_and_special_members_before_extraction(self):
        for name, kind in (("provider_core-0.2.0/../../outside", tarfile.REGTYPE),
                           ("/absolute", tarfile.REGTYPE), ("provider_core-0.2.0/link", tarfile.SYMTYPE),
                           ("provider_core-0.2.0/device", tarfile.CHRTYPE), ("provider_core-0.2.0/duplicate", "duplicate")):
            with self.subTest(name=name, kind=kind):
                path = self.root / "source.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.mode = 0o644
                    if kind == "duplicate":
                        archive.addfile(member, io.BytesIO())
                    else:
                        member.type = kind
                    archive.addfile(member, io.BytesIO())
                destination = self.root / "extracted"
                with self.assertRaisesRegex(ValueError, "source archive"):
                    unpack_source(path, destination, "0.2.0")
                self.assertFalse(destination.exists())

    def test_source_inventory_detects_changed_bytes_and_omitted_test_files(self):
        source = self.root / "source"
        (source / "src/provider_core").mkdir(parents=True)
        (source / "tests").mkdir()
        for name in ("README.md", "MANIFEST.in", "pyproject.toml", "src/provider_core/__init__.py", "tests/test_contract.py"):
            (source / name).write_text("authored\n")
        inventory = {path.relative_to(source).as_posix(): {"sha256": digest(path), "size": path.stat().st_size,
                     "mode": path.stat().st_mode & 0o777} for path in source.rglob("*") if path.is_file()}
        metadata = source / "src/provider_core.egg-info"
        metadata.mkdir()
        for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt", "top_level.txt"):
            (metadata / name).write_text("")
        for name in ("PKG-INFO", "setup.cfg"):
            (source / name).write_text("")
        self.assertEqual({"provider_core/__init__.py": b"authored\n"}, source_payload(source, inventory))
        (source / "tests/test_contract.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "pre-build source inventory"):
            source_payload(source, inventory)
        (source / "tests/test_contract.py").unlink()
        with self.assertRaisesRegex(ValueError, "pre-build source inventory"):
            source_payload(source, inventory)

    def test_wheel_payload_detects_missing_extra_or_changed_runtime_files(self):
        path = self.root / "core.whl"
        expected = {"provider_core/__init__.py": b"owned\n", "provider_core/delivery_client.py": b"client\n"}
        for payload in ({"provider_core/__init__.py": b"owned\n"}, expected | {"tools/helper.py": b"leak"},
                        expected | {"provider_core/delivery_client.py": b"changed"}):
            with self.subTest(payload=payload):
                with zipfile.ZipFile(path, "w") as archive:
                    for name, value in payload.items():
                        archive.writestr(name, value)
                with self.assertRaisesRegex(ValueError, "wheel"):
                    wheel_payload(path, expected, "0.2.0")

    def test_behavioral_test_closure_keeps_runtime_lock_pure(self):
        runtime = self.root / "runtime.lock"
        test = self.root / "tests.lock"
        target = self.root / "installed-tests.lock"
        runtime.write_text("core-runtime==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
        test.write_text("test-only==2.0 \\\n    --hash=sha256:" + "b" * 64 + "\n")
        original = runtime.read_bytes()
        behavioral_install_lock(runtime, test, target)
        self.assertEqual(original, runtime.read_bytes())
        self.assertIn("core-runtime==1.0", target.read_text())
        self.assertIn("test-only==2.0", target.read_text())
        with self.assertRaisesRegex(ValueError, "overlap runtime packages"):
            behavioral_install_lock(runtime, runtime, target)
        self.assertEqual(original, runtime.read_bytes())

    def test_corrupt_release_manifest_refuses_before_rebuild_and_emits_no_qualification_receipt(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        for name in artifact_names("0.2.0"):
            (artifacts / name).write_text("{}\n")
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(ValueError, "release hashes"):
            check(artifacts, "0" * 40)
        self.assertEqual("", output.getvalue())

    def test_wheel_build_uses_writable_stage_not_read_only_example_mount(self):
        source = ROOT / "examples/dummy-provider"
        staged_source = self.root / "staged"
        output = self.root / "wheels"
        output.mkdir()
        original = {
            path.relative_to(source): digest(path)
            for path in source.rglob("*") if path.is_file()
        }

        def simulate_backend(command, *, check, env):
            self.assertTrue(check)
            self.assertIn("wheel", command)
            self.assertEqual(str(staged_source), command[-1])
            self.assertEqual(str(output), command[command.index("--wheel-dir") + 1])
            self.assertEqual("315532800", env["SOURCE_DATE_EPOCH"])
            (staged_source / "build/lib/dummy_provider").mkdir(parents=True)

        with patch("tools.core_environment.subprocess.run", side_effect=simulate_backend) as runner:
            actual = build_source_wheel(
                source=source,
                staged_source=staged_source,
                python=Path("/runtime/example-builder/environment/bin/python"),
                output=output,
            )

        runner.assert_called_once()
        self.assertEqual(staged_source, actual)
        self.assertEqual(
            original,
            {path.relative_to(source): digest(path) for path in source.rglob("*") if path.is_file()},
        )
        self.assertFalse((source / "build").exists())
        self.assertTrue((staged_source / "build/lib/dummy_provider").is_dir())

    def test_wheel_staging_makes_read_only_source_directories_writable(self):
        source = self.root / "source"
        package = source / "src/science"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("value = 1\\n")
        directories = (source, source / "src", package)
        for directory in directories:
            directory.chmod(0o555)
        self.addCleanup(lambda: [directory.chmod(0o755) for directory in directories])

        staged_source = self.root / "staged"
        with patch("tools.core_environment.subprocess.run"):
            build_source_wheel(
                source=source,
                staged_source=staged_source,
                python=Path("/runtime/builder/bin/python"),
                output=self.root,
            )

        self.assertEqual(
            [True] * len(directories),
            [(staged_source / directory.relative_to(source)).stat().st_mode & 0o200 != 0
             for directory in directories],
        )
        self.assertEqual([0o555] * len(directories), [directory.stat().st_mode & 0o777 for directory in directories])

    def test_wheel_builder_rejects_source_symlinks_and_in_tree_staging(self):
        source = self.root / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text("[project]\\nname='fixture'\\n")
        with patch("tools.core_environment.subprocess.run") as runner:
            with self.assertRaisesRegex(ValueError, "outside the input tree"):
                build_source_wheel(
                    source=source,
                    staged_source=source / "build",
                    python=Path("/runtime/builder/bin/python"),
                    output=self.root,
                )
            (source / "linked").symlink_to("pyproject.toml")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                build_source_wheel(
                    source=source,
                    staged_source=self.root / "staged",
                    python=Path("/runtime/builder/bin/python"),
                    output=self.root,
                )
        runner.assert_not_called()
        self.assertFalse((source / "build").exists())
        self.assertFalse((self.root / "staged").exists())

    def test_all_python_wheel_builds_use_one_staged_entrypoint(self):
        wheel_calls = []
        shared_callers = []

        for path in sorted((ROOT / "tools").glob("*.py")):
            module = ast.parse(path.read_text(), filename=str(path))
            for function in (node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)):
                for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                    if isinstance(call.func, ast.Name) and call.func.id == "build_source_wheel":
                        shared_callers.append((path.name, function.name))
                    if (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr == "run"
                        and any(isinstance(item, ast.Constant) and item.value == "wheel"
                                for item in ast.walk(call))
                    ):
                        wheel_calls.append((path.name, function.name))

        self.assertEqual([("core_environment.py", "build_source_wheel")], wheel_calls)
        self.assertEqual(
            [("core_environment.py", "_build_candidate"),
             ("provider_core_tests.py", "_build_and_install_example")],
            sorted(shared_callers),
        )
