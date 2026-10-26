"""Complete authored image source discovery and independent reviewed inventory.

API Compose build-service and checker-image assembly assertions are specific to
that source repository. NMRNet has direct Dockerfile consumers and a launcher
that mounts source trees; its corresponding completeness assertion reads the
launcher's printed mount grant without contacting the container engine.
"""

from tools.container_guard import require_container

require_container()

# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_source_inventory.py
# The four original discovery/error fixture scenarios are retained. Container exact inventory,
# independent authority and deterministic diagnostic serialization retained.
# There are no checked-in Compose service/build files. Generated runtime recipes
# have separate rendering and deployment checks; this inventory owns image sources.
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import sources
from .source_inventory import SourceInventoryError, discover_source_files, inventory_delta, require_exact_inventory
from .sources import ROOT

def has_external_frontend(source):
    """Reject frontend directives even when hidden behind introductory comments."""
    return re.search(r"(?im)^\s*#\s*syntax\s*=", source) is not None


ROOT_ENTRY_MANIFEST = Path("/repository-source-inventory/root-entries")
# The coordinator explicitly keeps checkout control, credentials and writable
# acquisition/runtime state outside source-check authority. Capturing their
# names in the manifest proves classification without exposing their contents.
PRIVATE_ROOT_ENTRIES = frozenset({".git", ".agents", ".codex", ".aws", ".work", "secrets"})


def root_entry_visibility_findings(manifest, root):
    """Reject omitted public entries and root links hidden by engine binds.

    The contained runner captures type/name metadata using host find, with
    %y-tab-%f-NUL records. This retains the source suite's complete tree proof
    while granting only public source contents to the checker.
    """
    records = manifest.split("\0")
    if not records or records[-1] != "":
        raise ValueError("root-entry manifest must end with NUL")
    expected = {}
    findings = []
    for record in records[:-1]:
        kind, separator, name = record.partition("\t")
        if not separator or not name or "/" in name or name in {".", ".."}:
            raise ValueError("root-entry manifest has an invalid root name")
        if name in expected:
            raise ValueError("root-entry manifest repeats a root name")
        expected[name] = kind
        if name in PRIVATE_ROOT_ENTRIES:
            continue
        if kind not in {"f", "d"}:
            findings.append(f"{name}: public source root is not a regular file or directory")
            continue
        path = root / name
        if not path.exists():
            findings.append(f"{name}: public source root is invisible to repository checks")
        elif path.is_symlink() or (kind == "f" and not path.is_file()) or (kind == "d" and not path.is_dir()):
            findings.append(f"{name}: visible source root type disagrees with host inventory")
    for path in sorted(root.iterdir()):
        if path.name not in expected:
            findings.append(f"{path.name}: visible root entry is absent from host inventory")
    return findings


class SourceInventoryUnitTests(unittest.TestCase):
    def test_python_source_selection_respects_explicit_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "tools/z.py",
                "tools/nested/a.py",
                "tools/readme.txt",
                "tests/unit.py",
                "tests/repository/check.py",
                "src/nested/future.py",
                "packages/example/src/example/owner.py",
                "packages/example/tests/test_owner.py",
                "unowned/ignored.py",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pass\n")
            with patch.object(sources, "ROOT", root):
                self.assertEqual(
                    [
                        root / "packages/example/src/example/owner.py",
                        root / "src/nested/future.py",
                        root / "tools/nested/a.py",
                        root / "tools/z.py",
                    ],
                    sources.python_sources(production_only=True),
                )
                self.assertEqual(
                    [
                        root / "packages/example/src/example/owner.py",
                        root / "packages/example/tests/test_owner.py",
                        root / "src/nested/future.py",
                        root / "tests/repository/check.py",
                        root / "tests/unit.py",
                        root / "tools/nested/a.py",
                        root / "tools/z.py",
                    ],
                    sources.python_sources(production_only=False),
                )
                with self.assertRaises(TypeError):
                    sources.python_sources()

    def test_frontend_ownership_rejects_pinned_and_unpinned_registry_directives(self):
        self.assertFalse(has_external_frontend("# Local engine frontend.\nFROM base@sha256:hash\n"))
        for directive in ("# syntax=docker/dockerfile:1", "# SYNTAX = docker/dockerfile:1@sha256:" + "a" * 64):
            self.assertTrue(has_external_frontend("# Introduction.\n" + directive + "\nFROM base\n"))

    def test_root_visibility_reports_new_source_caches_and_hidden_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "stale.txt").write_text("fixture")
            manifest = "d\ttools\0f\tnew-source.py\0d\t.pytest_cache\0l\tlinked.py\0d\t.work\0d\t.aws\0d\tsecrets\0"
            self.assertEqual(
                [
                    "new-source.py: public source root is invisible to repository checks",
                    ".pytest_cache: public source root is invisible to repository checks",
                    "linked.py: public source root is not a regular file or directory",
                    "stale.txt: visible root entry is absent from host inventory",
                ],
                root_entry_visibility_findings(manifest, root),
            )

    def test_root_visibility_rejects_malformed_duplicate_and_mismatched_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest in ("f\tname", "f\t../escape\0", "f\tname\0f\tname\0", "f\t\0"):
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    root_entry_visibility_findings(manifest, root)
            (root / "file").write_text("fixture")
            self.assertEqual(
                ["file: visible source root type disagrees with host inventory"],
                root_entry_visibility_findings("d\tfile\0", root),
            )

    def test_discovery_is_recursive_and_inventory_delta_is_directional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose" / "nested").mkdir(parents=True)
            (root / "compose" / "base.yml").write_text("services: {}\n")
            (root / "compose" / "nested" / "debug.yaml").write_text("services: {}\n")

            discovered = discover_source_files(root=root, subtree="compose")

        self.assertEqual(
            (
                "compose/base.yml",
                "compose/nested/debug.yaml",
            ),
            discovered,
        )
        self.assertEqual(
            (
                ("compose/missing.yml",),
                ("compose/nested/debug.yaml",),
            ),
            inventory_delta(
                discovered=discovered,
                reviewed=("compose/base.yml", "compose/missing.yml"),
            ),
        )

    def test_exact_inventory_names_missing_and_unclassified_files(self) -> None:
        with self.assertRaisesRegex(
            SourceInventoryError,
            re.escape("compose source inventory is not exact; missing=['compose/missing.yml']; unclassified=['compose/new.yml']"),
        ):
            require_exact_inventory(
                label="compose source",
                discovered=("compose/base.yml", "compose/new.yml"),
                reviewed=("compose/base.yml", "compose/missing.yml"),
            )

    def test_discovery_rejects_file_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose"
            compose.mkdir()
            target = root / "target.yml"
            target.write_text("services: {}\n")
            (compose / "linked.yml").symlink_to(target)

            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose/linked.yml must not be a symlink",
            ):
                discover_source_files(root=root, subtree="compose")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose"
            compose.mkdir()
            target = root / "target"
            target.mkdir()
            (compose / "linked").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose/linked must not be a symlink",
            ):
                discover_source_files(root=root, subtree="compose")

    def test_discovery_rejects_invalid_or_missing_source_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for subtree in ("", "/compose", "compose/", "compose/../other"):
                with self.subTest(subtree=subtree), self.assertRaises((TypeError, ValueError)):
                    discover_source_files(root=root, subtree=subtree)
            with self.assertRaisesRegex(
                SourceInventoryError,
                "compose source root must be a directory",
            ):
                discover_source_files(root=root, subtree="compose")


class SourceInventoryRepositoryTests(unittest.TestCase):
    def test_host_root_inventory_is_complete_and_visible_to_source_checks(self):
        self.assertTrue(ROOT_ENTRY_MANIFEST.is_file(), msg="repository checks require a read-only host root-entry inventory")
        findings = root_entry_visibility_findings(ROOT_ENTRY_MANIFEST.read_bytes().decode("utf-8"), ROOT)
        self.assertEqual([], findings, msg="\n".join(findings))

    def test_authored_python_roots_are_complete_safe_trees(self):
        for subtree in ("tools", "tests"):
            with self.subTest(subtree=subtree):
                self.assertTrue(discover_source_files(root=ROOT, subtree=subtree))

    def test_repository_checker_grants_complete_read_only_source_trees(self):
        result = subprocess.run(
            ["bash", str(ROOT / "tools/repository.sh"), "--print", "tests/repository"], text=True, capture_output=True, check=True
        )
        arguments = shlex.split(result.stdout)
        mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
        for subtree in ("tools", "tests", "requirements", "containers", "notes"):
            self.assertIn(f"type=bind,src={ROOT / subtree},dst=/workspace/{subtree},readonly", mounts)
        for name in ("Makefile", "README.md"):
            self.assertIn(f"type=bind,src={ROOT / name},dst=/workspace/{name},readonly", mounts)
        self.assertFalse(any(f"src={ROOT / 'secrets'}" in mount for mount in mounts))
        self.assertNotIn(f"type=bind,src={ROOT / '.git'},dst=/workspace/.git,readonly", mounts)
        for mount in mounts:
            self.assertNotIn("docker.sock", mount)
            self.assertTrue(mount.endswith(",readonly"), msg=mount)
