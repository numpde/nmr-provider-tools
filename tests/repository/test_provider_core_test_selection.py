"""Focused provider-core unit selections exclude the native qualification lane."""

from tools.container_guard import require_container

require_container()

import pathlib
import unittest
from unittest.mock import patch

from sdk_build import provider_core_tests


class ProviderCoreTestSelectionTests(unittest.TestCase):
    def test_explicit_filename_runs_only_unit_tests(self):
        cases = (
            (
                ("test_type_requirements.py",),
                (("unit", ("test_type_requirements.py",)),),
            ),
            (
                ("test*.py",),
                (("unit", ("test*.py",)), ("native", ("test*.py",))),
            ),
        )

        for (patterns, expected) in cases:
            with self.subTest(patterns=patterns):
                candidate = pathlib.Path("provider_core-0.6.19-py3-none-any.whl")

                with (
                    patch.object(
                        provider_core_tests,
                        "build_candidate",
                        return_value=(candidate, pathlib.Path("install.lock"), {}),
                    ),
                    patch.object(
                        provider_core_tests,
                        "behavioral_install_lock",
                        return_value=pathlib.Path("behavioral.lock"),
                    ),
                    patch.object(
                        provider_core_tests,
                        "install_environment",
                        return_value=pathlib.Path("/runtime/python"),
                    ) as install,
                    patch.object(provider_core_tests.shutil, "copytree"),
                    patch.object(provider_core_tests, "_run_installed_suite") as run,
                ):
                    provider_core_tests.run_suite(suite="unit", patterns=patterns)

                actual = [
                    (invocation.kwargs["selection"], invocation.kwargs["patterns"])
                    for invocation in run.call_args_list
                ]

                self.assertEqual(expected, tuple(actual))
                self.assertEqual(len(expected), install.call_count)
