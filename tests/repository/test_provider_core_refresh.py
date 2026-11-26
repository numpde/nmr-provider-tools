"""SDK lock updates follow direct intent and stage complete environments."""

from tools.container_guard import require_container

require_container()

import json
import pathlib
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from sdk_build import refresh_locks


class ProviderCoreRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.package = self.root / "packages/provider-core"
        self.requirements = self.root / "requirements"
        self.output = self.root / "locks"
        self.pool = self.root / "pool"

        (self.package / "tests").mkdir(parents=True)
        self.requirements.mkdir()
        self.output.mkdir()
        self.pool.mkdir()

        (self.package / "pyproject.toml").write_text(
            '[build-system]\n'
            'requires = ["backend==2"]\n'
            '[project]\n'
            'requires-python = ">=3.12"\n'
            'dependencies = ["model==4"]\n'
            '[project.optional-dependencies]\n'
            'api = ["transport==5"]\n',
        )

        (self.package / "tests/requirements.in").write_text("# direct tests\n" "hypothesis==6.150.0\n")

        (self.requirements / "acquisition-bootstrap.toml").write_text(
            '[project]\n'
            'dependencies = ["bootstrap==1"]\n'
            '[tool.wheelhouse]\n'
            'index = "https://pypi.org/simple"\n'
            '[tool.wheelhouse.sources]\n'
            'bootstrap = { wheel = "https://example.invalid/bootstrap-1-py3-none-any.whl", sha256 = "' + "a" * 64 + '" }\n',
        )

        (self.requirements / "wheelhouse-tool-release.json").write_text(json.dumps({"name": "wheelhouse.pyz"}))

    def test_roles_follow_actual_direct_declarations_and_preserve_bootstrap_sources(self):
        projects = {role: tomllib.loads(content) for (role, content) in refresh_locks.projects(self.root).items()}

        self.assertEqual(["model==4", "transport==5"], projects["provider-core-runtime"]["project"]["dependencies"])
        self.assertEqual(["backend==2"], projects["provider-core-build"]["project"]["dependencies"])
        self.assertEqual(["model==4", "transport==5", "hypothesis==6.150.0"], projects["provider-core-test"]["project"]["dependencies"])
        self.assertEqual("a" * 64, projects["acquisition-bootstrap"]["tool"]["wheelhouse"]["sources"]["bootstrap"]["sha256"])

    def test_legacy_hash_locks_do_not_control_refresh(self):
        for legacy_lock in (self.requirements / "provider-core.lock", self.package / "tests/requirements.lock"):
            legacy_lock.write_text("obsolete-or-invalid-release-evidence")

        package = self.package / "pyproject.toml"

        package.write_text(package.read_text().replace("model==4", "model==8"))
        runtime = tomllib.loads(refresh_locks.projects(self.root)["provider-core-runtime"])

        self.assertEqual(["model==8", "transport==5"], runtime["project"]["dependencies"])

    def test_independent_tool_prepares_exact_bootstrap_and_stages_four_complete_roles(self):
        calls = []

        def execute_wheelhouse(arguments, **options):
            calls.append(arguments)
            project = pathlib.Path(arguments[arguments.index("--project") + 1])

            self.assertIn("project", tomllib.loads(project.read_text()))

            if "--lock" in arguments:
                destination = pathlib.Path(arguments[arguments.index("--lock") + 1])

                self.assertEqual(self.output, destination.parent)
                destination.write_text("staged complete lock")

        with patch.object(refresh_locks, "acquire_tool", return_value=self.pool / "wheelhouse.pyz"), patch.object(refresh_locks.subprocess, "run", side_effect=execute_wheelhouse):
            refresh_locks.refresh(self.root, self.pool, self.output)

        self.assertEqual(4, len(list(self.output.glob("*.wheelhouse.json"))))
        self.assertEqual(["lock", "lock", "lock", "acquire", "lock"], [call[4] for call in calls])
        self.assertEqual("bootstrap", calls[-2][calls[-2].index("--package") + 1])

    def test_resolution_failure_stops_before_later_roles(self):
        with patch.object(refresh_locks, "acquire_tool", return_value=self.pool / "wheelhouse.pyz"), patch.object(refresh_locks.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["wheelhouse", "lock"])) as resolution_process:
            with self.assertRaises(subprocess.CalledProcessError):
                refresh_locks.refresh(self.root, self.pool, self.output)

        self.assertEqual(1, resolution_process.call_count)
        self.assertFalse(list(self.output.iterdir()))


class CheckerResolverSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.recipe = self.root / "Dockerfile"
        self.base = pathlib.Path(__file__).resolve().parents[2] / "operations/docker/base_image.sh"

    def source_image(self):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; checker_resolver_image "$2"', "bash", str(self.base), str(self.recipe)],
            capture_output=True,
            text=True,
        )

    def test_preload_follows_the_current_dockerfile_pin(self):
        for digest in ("a" * 64, "b" * 64):
            with self.subTest(digest=digest):
                image = "ghcr.io/astral-sh/uv:0.10.0@sha256:" + digest

                self.recipe.write_text("COPY --from=" + image + " /uv /usr/local/bin/uv\n")
                resolver_source_process = self.source_image()

                self.assertEqual(0, resolver_source_process.returncode, msg=resolver_source_process.stderr)
                self.assertEqual(image + "\n", resolver_source_process.stdout)

    def test_missing_ambiguous_or_unpinned_resolver_source_fails(self):
        pinned = "COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:" + "a" * 64 + " /uv /usr/local/bin/uv\n"

        for recipe in ("", pinned + pinned, "COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv\n"):
            with self.subTest(recipe=recipe):
                self.recipe.write_text(recipe)
                resolver_source_process = self.source_image()

                self.assertEqual(2, resolver_source_process.returncode)
                self.assertIn("one pinned uv image", resolver_source_process.stderr)
                self.assertFalse(resolver_source_process.stdout)
