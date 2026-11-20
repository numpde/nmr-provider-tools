"""Focused SDK selections keep optional dependencies out of native tests."""

from tools.container_guard import require_container

require_container()

import pathlib
import unittest
from unittest.mock import patch

from sdk_build import provider_core_tests


class ProviderCoreTestSelectionTests(unittest.TestCase):
    def test_native_selections_exclude_optional_dependencies(self):
        cases = [
            {
                "suite": "unit",
                "patterns": ("test_type_requirements.py",),
                "expected_runs": [
                    {"selection": "unit", "patterns": ("test_type_requirements.py",)},
                ],
                "expected_locks": ["behavioral.lock"],
            },
            {
                "suite": "unit",
                "patterns": ("test*.py",),
                "expected_runs": [
                    {"selection": "unit", "patterns": ("test*.py",)},
                    {"selection": "native", "patterns": ("test*.py",)},
                ],
                "expected_locks": ["behavioral.lock", "provider_core-0.6.19-native-install.lock"],
            },
            {
                "suite": "native",
                "patterns": ("test_typed_contribution.py",),
                "expected_runs": [
                    {"selection": "native", "patterns": ("test_typed_contribution.py",)},
                ],
                "expected_locks": ["provider_core-0.6.19-native-install.lock"],
            },
            {
                "suite": "native",
                "patterns": ("test*.py",),
                "expected_runs": [
                    {"selection": "native", "patterns": ("test*.py",)},
                ],
                "expected_locks": ["provider_core-0.6.19-native-install.lock"],
            },
        ]

        for case in cases:
            with self.subTest(suite=case["suite"], patterns=case["patterns"]):
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
                    provider_core_tests.run_suite(suite=case["suite"], patterns=case["patterns"])

                actual_runs = [
                    {
                        "selection": invocation.kwargs["selection"],
                        "patterns": invocation.kwargs["patterns"],
                    }
                    for invocation in installed_suite_invocations.call_args_list
                ]

                self.assertEqual(case["expected_runs"], actual_runs)

                self.assertEqual(
                    case["expected_locks"],
                    [invocation.args[1].name for invocation in install.call_args_list],
                )

                if case["suite"] == "native":
                    behavioral_lock_merge.assert_not_called()
                else:
                    behavioral_lock_merge.assert_called_once()
