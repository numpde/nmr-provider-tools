"""Keep the dummy provider a standalone, copyable consumer example."""

from tools.container_guard import require_container

require_container()

import ast
import tomllib
import unittest

from tests.repository.quality.sources import ROOT


class DummyProviderExampleTests(unittest.TestCase):
    def test_example_is_an_independent_provider_package(self) -> None:
        example = ROOT / "examples/dummy-provider"
        project = tomllib.loads((example / "pyproject.toml").read_text())
        core = tomllib.loads(
            (ROOT / "packages/provider-core/pyproject.toml").read_text()
        )
        symlinks = sorted(
            path.relative_to(example).as_posix()
            for path in example.rglob("*")
            if path.is_symlink()
        )
        self.assertEqual([], symlinks)
        self.assertEqual(
            "dummy-provider-example",
            project["project"]["name"],
        )
        self.assertEqual(
            [f"provider-core=={core['project']['version']}"],
            project["project"]["dependencies"],
        )

    def test_runtime_source_does_not_import_testing_or_hosting(self) -> None:
        source = ROOT / "examples/dummy-provider/src/dummy_provider"
        forbidden = {
            "provider_core.testing",
            "provider_core.embedded_host",
            "provider_core.hosting",
        }
        findings = []

        for path in sorted(source.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                for name in names:
                    if any(
                        name == owner or name.startswith(owner + ".")
                        for owner in forbidden
                    ):
                        findings.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
                        )

        self.assertEqual([], findings, msg="\n".join(findings))

if __name__ == "__main__":
    unittest.main()
