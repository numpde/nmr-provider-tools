"""Focused SDK selections keep optional dependencies out of native tests."""

from tools.container_guard import require_container

require_container()

import pathlib
import unittest
from unittest.mock import patch

from sdk_build import provider_core_tests


class ProviderCoreTestSelectionTests(unittest.TestCase):
    def test_native_selections_exclude_optional_dependencies(self):
        cases = (
            (
                "unit",
                ("test_type_requirements.py",),
                (("unit", ("test_type_requirements.py",)),),
                ["behavioral.lock"],
            ),
            (
                "unit",
                ("test*.py",),
                (("unit", ("test*.py",)), ("native", ("test*.py",))),
                ["behavioral.lock", "provider_core-0.6.19-native-install.lock"],
            ),
            (
                "native",
                ("test_typed_contribution.py",),
                (("native", ("test_typed_contribution.py",)),),
                ["provider_core-0.6.19-native-install.lock"],
            ),
            (
                "native",
                ("test*.py",),
                (("native", ("test*.py",)),),
                ["provider_core-0.6.19-native-install.lock"],
            ),
        )

        for (suite, patterns, expected, expected_locks) in cases:
            with self.subTest(suite=suite, patterns=patterns):
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
                    ) as behavioral_lock_merge,
                    patch.object(
                        provider_core_tests,
                        "install_environment",
                        return_value=pathlib.Path("/runtime/python"),
                    ) as install,
                    patch.object(provider_core_tests.shutil, "copytree"),
                    patch.object(provider_core_tests, "_run_installed_suite") as installed_suite_invocations,
                ):
                    provider_core_tests.run_suite(suite=suite, patterns=patterns)

                actual = [
                    (invocation.kwargs["selection"], invocation.kwargs["patterns"])
                    for invocation in installed_suite_invocations.call_args_list
                ]

                self.assertEqual(expected, tuple(actual))

                self.assertEqual(
                    expected_locks,
                    [invocation.args[1].name for invocation in install.call_args_list],
                )

                if suite == "native":
                    behavioral_lock_merge.assert_not_called()
                else:
                    behavioral_lock_merge.assert_called_once()
