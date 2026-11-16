"""Catch common ways to reintroduce an implicit default Docker daemon.

The launcher tests exercise the selection behavior. This source check catches
literal bypasses; it cannot prove the behavior of arbitrary computed commands.
"""

from tools.container_guard import require_container

require_container()

import re
import unittest

from .sources import ROOT, source_files


_PATTERNS = (
    re.compile(r'''--context(?:=|\s+)["']?[A-Za-z][A-Za-z0-9_.-]*\b'''),
    re.compile(r'''\bcontext\s+inspect\s+["']?[A-Za-z][A-Za-z0-9_.-]*\b'''),
    re.compile(r'''\bDOCKER_CONTEXT\s*[:?+]?=\s*["']?[A-Za-z][A-Za-z0-9_.-]*\b'''),
    re.compile(r'''["']DOCKER_CONTEXT["']\s*:\s*["'][A-Za-z][A-Za-z0-9_.-]*["']'''),
    re.compile(r'''["']--context["']\s*,\s*["'][A-Za-z][A-Za-z0-9_.-]*["']'''),
    re.compile(r'''\$\{DOCKER_CONTEXT(?::?-)["']?default'''),
    re.compile(r'''\bdocker\s+(?:run|ps|build|inspect|compose|stop|rm|pull|image|volume|exec|logs|info)\b'''),
    re.compile(r'''unix:///[A-Za-z0-9_.-]+'''),
)


def implicit_defaults(source):
    return [number for (number, line) in enumerate(source.splitlines(), 1)
            if not line.lstrip().startswith("#") and any(pattern.search(line) for pattern in _PATTERNS)]


class DockerContextPostureTests(unittest.TestCase):
    def test_recognizes_common_implicit_defaults(self):
        for source in (
            "docker --context default ps",
            "docker --context production ps",
            'argv = ["--context", "production"]',
            "docker ps",
            "docker --host unix:///var/run/docker.sock ps",
            "docker context inspect default",
            "DOCKER_CONTEXT ?= default",
            "DOCKER_CONTEXT := production",
            'environment = {"DOCKER_CONTEXT": "default"}',
            'context=${DOCKER_CONTEXT:-default}',
        ):
            with self.subTest(source=source):
                self.assertEqual([1], implicit_defaults(source))

    def test_explicit_default_choice_is_allowed(self):
        self.assertEqual([], implicit_defaults('if [[ $name == default ]]; then inspect_selected_context; fi'))
        self.assertEqual([], implicit_defaults('# Run make test DOCKER_CONTEXT=default'))

    def test_runtime_sources_do_not_force_default(self):
        patterns = ["Makefile"]

        patterns.extend(f"{directory}/**/{pattern}" for directory in ("tools", "packages/provider-core/src", "containers") for pattern in ("*.py", "*.sh", "Dockerfile"))

        paths = source_files(root=ROOT, patterns=patterns)

        failures = [f"{path.relative_to(ROOT)}:{line}" for path in paths
                    for line in implicit_defaults(path.read_text(encoding="utf-8"))]

        self.assertEqual([], failures)
