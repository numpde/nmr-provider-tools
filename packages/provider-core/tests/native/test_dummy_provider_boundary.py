"""Keep the dummy provider limited to science and its contribution adapter."""

import ast
from importlib import resources
import unittest

import provider_core.examples.dummy_provider as dummy_provider
import provider_core.examples.dummy_provider.binding as dummy_binding


class DummyProviderBoundaryTests(unittest.TestCase):
    def test_provider_contains_only_science_and_adapter(self):
        root = resources.files(dummy_provider)
        sources = {item.name for item in root.iterdir() if item.name.endswith(".py")}

        self.assertEqual({"__init__.py", "binding.py", "science.py"}, sources)

        provider_core_imports = set()
        for name in sources:
            tree = ast.parse(
                root.joinpath(name).read_text(encoding="utf-8"),
                filename=name,
            )
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

        self.assertEqual(
            {
                "provider_core.contribution",
                "provider_core.contribution_declaration",
            },
            provider_core_imports,
        )

    def test_adapter_exports_only_binding_factories(self):
        self.assertEqual(
            (
                "sum_binding",
                "weighted_sum_binding",
            ),
            dummy_binding.__all__,
        )


if __name__ == "__main__":
    unittest.main()
