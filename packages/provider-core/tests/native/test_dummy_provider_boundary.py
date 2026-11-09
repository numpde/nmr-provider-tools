"""Keep the dummy provider limited to science and its contribution adapter."""

import ast
from importlib import resources
import unittest

from provider_core.contribution import ScientificRejection
import provider_core.examples.dummy_provider as dummy_provider
from provider_core.examples.dummy_provider import science


class DummyProviderBoundaryTests(unittest.TestCase):
    def test_provider_contains_only_science_and_adapter(self):
        root = resources.files(dummy_provider)
        sources = {item.name for item in root.iterdir() if item.name.endswith(".py")}

        self.assertEqual({"__init__.py", "binding.py", "science.py"}, sources)

        expected_imports = {
            "__init__.py": set(),
            "science.py": {"provider_core.contribution"},
            "binding.py": {
                "provider_core.contribution",
                "provider_core.contribution_declaration",
            },
        }
        for name in sources:
            tree = ast.parse(
                root.joinpath(name).read_text(encoding="utf-8"),
                filename=name,
            )
            provider_core_imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    provider_core_imports.update(
                        alias.name
                        for alias in node.names
                        if alias.name.startswith("provider_core")
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("provider_core")
                ):
                    provider_core_imports.add(node.module)

            with self.subTest(name=name):
                self.assertEqual(expected_imports[name], provider_core_imports)

    def test_scientific_rejection_uses_the_shared_contract(self):
        self.assertTrue(
            issubclass(
                science.ScientificInputRejected,
                ScientificRejection,
            )
        )


if __name__ == "__main__":
    unittest.main()
