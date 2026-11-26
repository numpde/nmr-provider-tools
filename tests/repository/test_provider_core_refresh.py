"""SDK lock updates follow direct intent and stage complete environments."""

from tools.container_guard import require_container

require_container()

import pathlib
import subprocess
import tempfile
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

    def test_roles_follow_actual_direct_declarations(self):
        projects = {role: content.splitlines() for (role, content) in refresh_locks.projects(self.root).items()}

        self.assertEqual(["model==4", "transport==5"], projects["provider-core-runtime"])
        self.assertEqual(["backend==2"], projects["provider-core-build"])
        self.assertEqual(["hypothesis==6.150.0"], projects["provider-core-test"])

    def test_existing_locks_do_not_control_refresh(self):
        for lock in (self.requirements / "provider-core.lock", self.package / "tests/requirements.lock"):
            lock.write_text("obsolete-or-invalid-pins")

        package = self.package / "pyproject.toml"
        package.write_text(package.read_text().replace("model==4", "model==8"))
        self.assertEqual(["model==8", "transport==5"], refresh_locks.projects(self.root)["provider-core-runtime"].splitlines())

    def test_uv_stages_all_three_hashed_requirements_locks(self):
        def resolve(command, **options):
            self.assertIn("--generate-hashes", command)
            self.assertIn("--only-binary=:all:", command)
            project = pathlib.Path(command[5])

            self.assertTrue(project.read_text())
            destination = pathlib.Path(command[command.index("--output-file") + 1])

            self.assertEqual(self.output, destination.parent)
            destination.write_text("complete hashed lock")

        with patch.object(refresh_locks.subprocess, "run", side_effect=resolve) as resolver:
            refresh_locks.refresh(self.root, self.output)

        self.assertEqual(3, resolver.call_count)
        self.assertEqual(3, len(list(self.output.glob("*.lock"))))

    def test_resolution_failure_stops_before_later_roles(self):
        with patch.object(refresh_locks.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["uv", "pip", "compile"])) as resolver:
            with self.assertRaises(subprocess.CalledProcessError):
                refresh_locks.refresh(self.root, self.output)

        self.assertEqual(1, resolver.call_count)
        self.assertEqual([], list(self.output.iterdir()))


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
