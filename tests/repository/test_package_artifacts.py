"""Release qualification refuses omitted source and unsafe archive membership."""

from tools.container_guard import require_container

require_container()

import ast
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
import pathlib
from contextlib import redirect_stdout
from unittest.mock import call, patch

from operations.release.qualify import (
    artifact_names,
    check,
    qualify_acquisition_bundle,
    source_payload,
    unpack_source,
    wheel_payload,
)

from sdk_build.core_environment import behavioral_install_lock, build_source_wheel, normalize_sdist
from tools.wheelhouse import digest
from tools.artifact_metadata import metadata_mismatch
from tests.repository.quality.sources import ROOT, source_files


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def _acquisition_archive(self, archive_path, members):
        with tarfile.open(archive_path, "w:gz") as archive:
            for (name, payload, kind, mode) in members:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = mode

                if member.isfile():
                    member.size = len(payload)

                    archive.addfile(member, io.BytesIO(payload))
                else:
                    archive.addfile(member)

    def test_source_normalization_cleans_scratch_and_preserves_failed_input(self):
        for kind in (tarfile.REGTYPE, tarfile.SYMTYPE):
            with self.subTest(kind=kind):
                source_archive_path = self.root / "source.tar.gz"

                with tarfile.open(source_archive_path, "w:gz") as archive:
                    member = tarfile.TarInfo("package/member")
                    member.type = kind
                    member.uid = 123

                    archive.addfile(member, io.BytesIO())

                original = source_archive_path.read_bytes()

                if kind == tarfile.SYMTYPE:
                    with self.assertRaisesRegex(ValueError, "unsupported link"):
                        normalize_sdist(source_archive_path)

                    self.assertEqual(original, source_archive_path.read_bytes())
                else:
                    normalize_sdist(source_archive_path)

                    with tarfile.open(source_archive_path, "r:gz") as archive:
                        self.assertEqual(0, archive.getmembers()[0].uid)

                self.assertEqual([source_archive_path], list(self.root.iterdir()))

    def test_acquisition_sources_are_admitted_before_extraction(self):
        cases = (
            ("../escaped", b"x", tarfile.REGTYPE, 0o644),
            ("/absolute", b"x", tarfile.REGTYPE, 0o644),
            ("linked", b"", tarfile.SYMTYPE, 0o644),
            ("writable", b"x", tarfile.REGTYPE, 0o666),
        )

        for (index, (name, payload, kind, mode)) in enumerate(cases):
            with self.subTest(name=name):
                scratch = self.root / f"scratch-{index}"

                scratch.mkdir()
                archive_path = scratch / "sources.tar.gz"

                self._acquisition_archive(archive_path, ((name, payload, kind, mode),))

                expected = {
                    name: {
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                        "mode": 0o644,
                    },
                }

                with patch("operations.release.qualify.subprocess.run") as commands:
                    with self.assertRaises(ValueError):
                        qualify_acquisition_bundle(archive_path, scratch, expected)

                commands.assert_not_called()
                self.assertFalse((scratch / "acquisition-source").exists())
                self.assertFalse((scratch / "escaped").exists())

    def test_acquisition_sources_reject_duplicate_and_conflicting_paths(self):
        cases = (
            (("acquisition_tool/owner.sh", "acquisition_tool/owner.sh"), (b"a", b"b")),
            (("acquisition_tool", "acquisition_tool/owner.sh"), (b"a", b"b")),
        )

        for (index, (names, payloads)) in enumerate(cases):
            with self.subTest(names=names):
                scratch = self.root / f"conflict-{index}"

                scratch.mkdir()
                archive_path = scratch / "sources.tar.gz"

                members = [
                    (name, payload, tarfile.REGTYPE, 0o644)
                    for (name, payload) in zip(names, payloads, strict=True)
                ]

                self._acquisition_archive(archive_path, members)

                expected = {
                    name: {
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                        "mode": 0o644,
                    }
                    for (name, payload) in zip(names, payloads, strict=True)
                }

                with patch("operations.release.qualify.subprocess.run") as commands:
                    with self.assertRaises(ValueError):
                        qualify_acquisition_bundle(archive_path, scratch, expected)

                commands.assert_not_called()
                self.assertFalse((scratch / "acquisition-source").exists())

    def test_acquisition_sources_extract_verified_members(self):
        scratch = self.root / "scratch"

        scratch.mkdir()
        archive_path = scratch / "sources.tar.gz"

        members = (
            (
                "acquisition_tool/owner.sh",
                (
                    b"#!/bin/sh\n"
                    b"exit 0\n"
                ),
                tarfile.REGTYPE,
                0o644,
            ),
            ("provider_core/bound_http_proxy.py", b"PROXY = 1\n", tarfile.REGTYPE, 0o644),
        )

        self._acquisition_archive(archive_path, members)

        expected = {
            name: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "mode": mode,
            }
            for (name, payload, _, mode) in members
        }

        with patch("operations.release.qualify.subprocess.run") as commands:
            qualify_acquisition_bundle(archive_path, scratch, expected)

        extracted = scratch / "acquisition-source"

        self.assertEqual(
            [
                call(["bash", "-n", str(extracted / "acquisition_tool/owner.sh")], check=True),
                call(
                    [
                        "python3",
                        "-I",
                        "-S",
                        "-B",
                        str(extracted / "acquisition_tool/source_update.py"),
                        "verify",
                        "--source-dir",
                        str(extracted)
                    ],
                    check=True
                ),
                call(["python3", "-I", "-S", "-B", str(ROOT / "packages/acquisition/build/acquisition_import_check.py"), str(extracted)], check=True),
                call(["python3", "-I", "-S", "-B", str(extracted / "acquisition_tool/identity.py"), "--help"], check=True, stdout=subprocess.DEVNULL),
            ],
            commands.call_args_list
        )

        for (name, payload, _, _) in members:
            self.assertEqual(payload, (scratch / "acquisition-source" / name).read_bytes())

    def test_acquisition_sources_reject_changed_bytes_before_writing(self):
        scratch = self.root / "changed"

        scratch.mkdir()
        archive_path = scratch / "sources.tar.gz"
        name = "acquisition_tool/owner.sh"
        changed_bytes = b"after!"
        expected_bytes = b"before"

        self._acquisition_archive(
            archive_path,
            ((name, changed_bytes, tarfile.REGTYPE, 0o644),),
        )

        expected = {
            name: {
                "sha256": hashlib.sha256(expected_bytes).hexdigest(),
                "size": len(expected_bytes),
                "mode": 0o644,
            },
        }

        with patch("operations.release.qualify.subprocess.run") as commands:
            with self.assertRaises(ValueError):
                qualify_acquisition_bundle(archive_path, scratch, expected)

        commands.assert_not_called()
        self.assertFalse((scratch / "acquisition-source").exists())

    def test_cold_import_check_uses_the_extracted_source_tree(self):
        source = self.root / "cold-import"

        for name in ("linux_idmap/probe.py", "provider_core/bound_http_proxy.py"):
            source_file_path = source / name

            source_file_path.parent.mkdir(parents=True, exist_ok=True)
            source_file_path.write_text("QUALIFIED = True")

        check_script = ROOT / "packages/acquisition/build/acquisition_import_check.py"

        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(check_script),
            str(source),
        ]

        valid_imports = subprocess.run(command, capture_output=True, text=True, check=False)

        self.assertEqual(0, valid_imports.returncode, msg=valid_imports.stderr)

        (source / "linux_idmap/probe.py").unlink()
        missing_import = subprocess.run(command, capture_output=True, text=True, check=False)

        self.assertNotEqual(0, missing_import.returncode)

    def test_source_archive_refuses_traversal_links_duplicate_and_special_members_before_extraction(self):
        for (name, kind) in (
            (".", tarfile.REGTYPE),
            ("provider_core-0.2.0/../../outside", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("provider_core-0.2.0/link", tarfile.SYMTYPE),
            ("provider_core-0.2.0/device", tarfile.CHRTYPE),
            ("provider_core-0.2.0/duplicate", "duplicate")
        ):
            with self.subTest(name=name, kind=kind):
                source_archive_path = self.root / "source.tar.gz"

                with tarfile.open(source_archive_path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.mode = 0o644

                    if kind == "duplicate":
                        archive.addfile(member, io.BytesIO())
                    else:
                        member.type = kind

                    archive.addfile(member, io.BytesIO())

                destination = self.root / "extracted"

                with self.assertRaises(ValueError):
                    unpack_source(source_archive_path, destination, "0.2.0")

                self.assertFalse(destination.exists())

    def test_source_inventory_detects_changed_bytes_and_omitted_test_files(self):
        source = self.root / "source"

        (source / "src/provider_core").mkdir(parents=True)
        (source / "tests").mkdir()

        for name in ("README.md", "MANIFEST.in", "pyproject.toml", "src/provider_core/__init__.py", "tests/test_contract.py"):
            (source / name).write_text("authored\n")

        inventory = {
            source_file_path.relative_to(source).as_posix(): {
                "sha256": digest(source_file_path),
                "size": source_file_path.stat().st_size,
                "mode": source_file_path.stat().st_mode & 0o777
            }
            for source_file_path in source.rglob("*") if source_file_path.is_file()
        }

        metadata = source / "src/provider_core.egg-info"

        metadata.mkdir()

        for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt", "top_level.txt"):
            (metadata / name).write_text("")

        for name in ("PKG-INFO", "setup.cfg"):
            (source / name).write_text("")

        self.assertEqual({"provider_core/__init__.py": b"authored\n"}, source_payload(source, inventory))

        source_member = "tests/test_contract.py"
        source_file = source / source_member

        source_file.write_text("changed\n")

        with self.assertRaises(ValueError) as captured:
            source_payload(source, inventory)

        source_sha256 = digest(source_file)
        expected_sha256 = inventory[source_member]["sha256"]

        self.assertIn(source_member, str(captured.exception))
        self.assertIn(f"{source_sha256=}", str(captured.exception))
        self.assertIn(f"{expected_sha256=}", str(captured.exception))

        source_file.unlink()

        with self.assertRaises(ValueError) as captured:
            source_payload(source, inventory)

        self.assertIn(source_member, str(captured.exception))
        self.assertIn("regular file", str(captured.exception))

    def test_wheel_payload_detects_missing_extra_or_changed_runtime_files(self):
        wheel_path = self.root / "core.whl"
        expected = {"provider_core/__init__.py": b"owned\n", "provider_core/delivery_client.py": b"client\n"}

        for (payload, affected_member) in (
            ({"provider_core/__init__.py": b"owned\n"}, "provider_core/delivery_client.py"),
            (expected | {"tools/helper.py": b"leak"}, "tools/helper.py"),
            (expected | {"provider_core/delivery_client.py": b"changed"}, "provider_core/delivery_client.py"),
        ):
            with self.subTest(payload=payload):
                with zipfile.ZipFile(wheel_path, "w") as archive:
                    for (name, payload_bytes) in payload.items():
                        archive.writestr(name, payload_bytes)

                with self.assertRaises(ValueError) as rejected:
                    wheel_payload(wheel_path, expected, "0.2.0")

                self.assertIn(affected_member, str(rejected.exception))

    def test_behavioral_test_closure_keeps_runtime_lock_pure(self):
        runtime = self.root / "runtime.lock"
        test = self.root / "tests.lock"
        target = self.root / "installed-tests.lock"

        runtime.write_text(
            (
                "core-runtime==1.0 \\\n"
                "    --hash=sha256:"
            ) + "a" * 64 + "\n"
        )

        test.write_text(
            (
                "test-only==2.0 \\\n"
                "    --hash=sha256:"
            ) + "b" * 64 + "\n"
        )

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

        checksum_manifest_path = artifacts / "provider_core-0.2.0-sha256.json"
        captured_hashes = {
            artifact.name: digest(artifact)
            for artifact in artifacts.iterdir()
            if artifact != checksum_manifest_path
        }

        last_artifact_name = max(captured_hashes)

        for (recorded_hashes, expected_evidence) in (
            ({}, checksum_manifest_path.name),
            ([], checksum_manifest_path.name),
            (captured_hashes | {last_artifact_name: "0" * 64}, last_artifact_name),
        ):
            with self.subTest(recorded_hashes=recorded_hashes):
                checksum_manifest_path.write_text(json.dumps(recorded_hashes))
                qualification_output = io.StringIO()

                with redirect_stdout(qualification_output), self.assertRaises(ValueError) as rejected:
                    check(artifacts, "0" * 40)

                self.assertIn(expected_evidence, str(rejected.exception))
                self.assertEqual("", qualification_output.getvalue())

    def test_metadata_evidence_reveals_changed_bytes_and_unshared_hashes(self):
        recorded_bytes = b"prefix" * 100 + b"recorded" + b"suffix" * 100
        captured_bytes = b"prefix" * 100 + b"captured" + b"suffix" * 100
        common_hashes = {(str(index) * 64) for index in range(10)}

        for (recorded_metadata, captured_metadata, expected_evidence) in (
            (
                {"METADATA": recorded_bytes},
                {"METADATA": captured_bytes},
                {hashlib.sha256(recorded_bytes).hexdigest(), hashlib.sha256(captured_bytes).hexdigest()},
            ),
            (
                {("package", "1.0"): common_hashes | {"a" * 64}},
                {("package", "1.0"): common_hashes | {"b" * 64}},
                {"a" * 64, "b" * 64},
            ),
        ):
            with self.subTest(expected_evidence=expected_evidence):
                diagnostic = metadata_mismatch(recorded_metadata, captured_metadata)

                for evidence in expected_evidence:
                    self.assertIn(evidence, diagnostic)

    def test_wheel_build_uses_writable_stage_not_read_only_example_mount(self):
        source = ROOT / "examples/dummy-provider"
        staged_source = self.root / "staged"
        output = self.root / "wheels"

        output.mkdir()

        original = {
            source_file_path.relative_to(source): digest(source_file_path)
            for source_file_path in source.rglob("*") if source_file_path.is_file()
        }

        def simulate_backend(command, *, check, env):
            self.assertTrue(check)
            self.assertIn("wheel", command)
            self.assertEqual(str(staged_source), command[-1])
            self.assertEqual(str(output), command[command.index("--wheel-dir") + 1])
            self.assertEqual("315532800", env["SOURCE_DATE_EPOCH"])
            (staged_source / "build/lib/dummy_provider").mkdir(parents=True)

        with (
            patch("tools.adapters.pip_install.pip_command", return_value=["python", "-m", "pip"]),
            patch("sdk_build.core_environment.subprocess.run", side_effect=simulate_backend) as runner,
        ):
            actual = build_source_wheel(
                source=source,
                staged_source=staged_source,
                python=pathlib.Path("/runtime/example-builder/environment/bin/python"),
                output=output,
            )

        runner.assert_called_once()
        self.assertEqual(staged_source, actual)

        self.assertEqual(
            original,
            {
                source_file_path.relative_to(source): digest(source_file_path)
                for source_file_path in source.rglob("*") if source_file_path.is_file()
            },
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
            self.addCleanup(directory.chmod, 0o755)

        staged_source = self.root / "staged"

        with (
            patch("tools.adapters.pip_install.pip_command", return_value=["python", "-m", "pip"]),
            patch("sdk_build.core_environment.subprocess.run"),
        ):
            build_source_wheel(
                source=source,
                staged_source=staged_source,
                python=pathlib.Path("/runtime/builder/bin/python"),
                output=self.root,
            )

        for directory in directories:
            with self.subTest(directory=directory.relative_to(source)):
                staged_directory = staged_source / directory.relative_to(source)

                self.assertNotEqual(0, staged_directory.stat().st_mode & 0o200)
                self.assertEqual(0o555, directory.stat().st_mode & 0o777)

    def test_wheel_builder_rejects_source_symlinks_and_in_tree_staging(self):
        source = self.root / "source"

        source.mkdir()
        (source / "pyproject.toml").write_text("[project]\\nname='fixture'\\n")

        with patch("sdk_build.core_environment.subprocess.run") as runner:
            with self.assertRaisesRegex(ValueError, "outside the input tree"):
                build_source_wheel(
                    source=source,
                    staged_source=source / "build",
                    python=pathlib.Path("/runtime/builder/bin/python"),
                    output=self.root,
                )

            (source / "linked").symlink_to("pyproject.toml")

            with self.assertRaisesRegex(ValueError, "symlinks"):
                build_source_wheel(
                    source=source,
                    staged_source=self.root / "staged",
                    python=pathlib.Path("/runtime/builder/bin/python"),
                    output=self.root,
                )

        runner.assert_not_called()
        self.assertFalse((source / "build").exists())
        self.assertFalse((self.root / "staged").exists())

    def test_all_python_wheel_builds_use_one_staged_entrypoint(self):
        wheel_calls = []
        shared_callers = []

        source_paths = source_files(root=ROOT, patterns=("tools/*.py", "packages/provider-core/sdk_build/*.py"))

        for source_path in source_paths:
            module = ast.parse(source_path.read_text(), filename=str(source_path))

            for function in (node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)):
                for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                    if isinstance(call.func, ast.Name) and (call.func.id == "build_source_wheel"):
                        shared_callers.append((source_path.name, function.name))

                    if (
                        isinstance(call.func, ast.Attribute) and
                        (call.func.attr == "run") and
                        any(
                            ((isinstance(ast_node, ast.Constant) and (ast_node.value == "wheel")) or
                            (isinstance(ast_node, ast.Call) and
                             isinstance(ast_node.func, ast.Name) and
                             (ast_node.func.id == "build_wheel_command")))
                            for ast_node in ast.walk(call)
                        )
                    ):
                        wheel_calls.append((source_path.name, function.name))

        self.assertEqual([("core_environment.py", "build_source_wheel")], wheel_calls)

        self.assertEqual(
            [
                ("core_environment.py", "_build_candidate"),
                ("provider_core_tests.py", "_build_and_install_project")
            ],
            sorted(shared_callers),
        )
