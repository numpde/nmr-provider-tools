"""Flag generic bindings and function names so each name explains its role.

This deliberately strict first pass includes helpers, tests and public keyword
parameters. Findings require semantic review; they do not authorize renaming a
public API or serialized field. Reads and string contents are not declarations.
Exception aliases and mapping-rest captures point to their owning header.
"""

from tools.container_guard import require_container

require_container()

import ast
import pathlib
import unittest
from dataclasses import dataclass

from .sources import ROOT, python_sources


@dataclass(frozen=True, slots=True, kw_only=True)
class _Finding:
    line: int
    variable_name: str

    def diagnostic(self, *, source_path: pathlib.Path) -> str:
        return (
            f"{source_path}:{self.line}: generic name {self.variable_name!r}; "
            f"name its class or purpose; review public keyword and field contracts before renaming"
        )


def _generic_name(*, variable_name: str) -> bool:
    return variable_name.strip("_").casefold() in {
        "value",
        "values",
        "data",
        "document",
        "item",
        "items",
        "entries",
        "prepare",
        "process",
        "execute",
        "validate",
        "run",
        "label",
        "file",
        "filepath",
        "path",
        "relative_path",
        "rel_path",
        "result",
        "results",
        "obj",
        "objs",
        "tmp",
        "temp",
    }


def _findings(*, source: str) -> list[_Finding]:
    findings = set()

    for syntax_node in ast.walk(node=ast.parse(source=source)):
        bindings = []

        if isinstance(syntax_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.append((syntax_node.lineno, syntax_node.name))
        elif isinstance(syntax_node, ast.arg):
            bindings.append((syntax_node.lineno, syntax_node.arg))
        elif isinstance(syntax_node, ast.Name) and isinstance(syntax_node.ctx, ast.Store):
            bindings.append((syntax_node.lineno, syntax_node.id))
        elif (
            isinstance(syntax_node, ast.Attribute) and
            isinstance(syntax_node.ctx, ast.Store) and
            isinstance(syntax_node.value, ast.Name) and
            (syntax_node.value.id in {"self", "cls"})
        ):
            bindings.append((syntax_node.lineno, syntax_node.attr))
        elif (isinstance(syntax_node, ast.ExceptHandler)) and (syntax_node.name is not None):
            bindings.append((syntax_node.lineno, syntax_node.name))
        elif (isinstance(syntax_node, (ast.MatchAs, ast.MatchStar))) and (syntax_node.name is not None):
            bindings.append((syntax_node.lineno, syntax_node.name))
        elif (isinstance(syntax_node, ast.MatchMapping)) and (syntax_node.rest is not None):
            bindings.append((syntax_node.lineno, syntax_node.rest))
        elif isinstance(syntax_node, (ast.Import, ast.ImportFrom)):
            for imported_name in syntax_node.names:
                if imported_name.name == "*":
                    continue

                if imported_name.asname is not None:
                    bindings.append((imported_name.lineno, imported_name.asname))
                elif isinstance(syntax_node, ast.Import):
                    bindings.append((imported_name.lineno, imported_name.name.split(".")[0]))
                else:
                    bindings.append((imported_name.lineno, imported_name.name))

        for (binding_line, binding_name) in bindings:
            if _generic_name(variable_name=binding_name):
                findings.add((binding_line, binding_name))

    return [_Finding(line=line, variable_name=binding_name) for (line, binding_name) in sorted(findings)]


class GenericVariableNamePostureTests(unittest.TestCase):
    def test_generic_action_names_are_flagged_for_functions_and_variables(self):
        source = (
            "def prepare():\n"
            "    pass\n"
            "async def process():\n"
            "    pass\n"
            "execute = 1\n"
            "_VALIDATE = 2\n"
            "run = 3\n"
            "def prepare_input():\n"
            "    pass\n"
            "run_inference = 4\n"
        )

        self.assertEqual(
            first=["prepare", "process", "execute", "_VALIDATE", "run"],
            second=[finding.variable_name for finding in _findings(source=source)],
        )

    def test_entries_and_document_require_a_specific_purpose(self):
        source = (
            "entries = []\n"
            "_ENTRIES = []\n"
            "reference_entries = []\n"
            "document = {}\n"
            "_DOCUMENT = {}\n"
            "release_manifest = {}\n"
        )

        self.assertEqual(
            first=["entries", "_ENTRIES", "document", "_DOCUMENT"],
            second=[finding.variable_name for finding in _findings(source=source)],
        )

    def test_generic_path_and_file_names_are_flagged(self):
        source = (
            "path = 1\n"
            "relative_path = 2\n"
            "rel_path = 3\n"
            "file = 4\n"
            "filepath = 5\n"
            "acquisition_rel_path = 6\n"
        )

        self.assertEqual(
            first=["path", "relative_path", "rel_path", "file", "filepath"],
            second=[finding.variable_name for finding in _findings(source=source)],
        )

    def test_generic_parameters_assignments_and_comprehensions_are_flagged(self):
        source = (
            "def convert(value, /, *items, data, label, **values):\n"
            "    result = [item for item in items]\n"
            "    return result\n"
        )

        self.assertEqual(
            first=["data", "items", "label", "value", "values", "item", "result"],
            second=[finding.variable_name for finding in _findings(source=source)],
        )

    def test_underscores_and_case_do_not_hide_generic_names(self):
        source = (
            "_value = 1\n"
            "VALUES = []\n"
            "__result__ = None\n"
        )

        self.assertEqual(first=["_value", "VALUES", "__result__"], second=[finding.variable_name for finding in _findings(source=source)])

    def test_purpose_names_reads_keys_comments_and_strings_are_not_bindings(self):
        source = (
            'payload_bytes = data\n'
            'result_ref = {"value": "item"}\n'
            '# result = data\n'
            'provider.value = 1\n'
        )

        self.assertEqual(first=[], second=_findings(source=source))

    def test_attributes_imports_exception_and_pattern_bindings_are_checked(self):
        source = (
            "import data.part\n"
            "from package import value\n"
            "from package import thing as tmp\n"
            "class Record:\n"
            "    result: str\n"
            "    def method(self):\n"
            "        self.value = 1\n"
            "        cls.items = []\n"
            "try:\n"
            "    work()\n"
            "except Error as obj:\n"
            "    raise\n"
            "match observed:\n"
            "    case {'name': value, **data}:\n"
            "        pass\n"
            "    case [*items]:\n"
            "        pass\n"
        )

        self.assertEqual(
            first=["data", "value", "tmp", "result", "value", "items", "obj", "data", "value", "items"],
            second=[finding.variable_name for finding in _findings(source=source)],
        )

    def test_multiline_import_diagnostics_locate_each_binding(self):
        source = (
            "from package import (\n"
            "    value,\n"
            "    data,\n"
            ")\n"
        )

        self.assertEqual(
            first=[(2, "value"), (3, "data")],
            second=[(finding.line, finding.variable_name) for finding in _findings(source=source)],
        )

    def test_control_bindings_are_checked_once_at_each_declaration(self):
        source = (
            "for (item, value) in entries:\n"
            "    use(item, value)\n"
            "with context() as data:\n"
            "    use(data)\n"
            "if (result := run()):\n"
            "    use(result)\n"
        )

        self.assertEqual(first=["item", "value", "data", "result"], second=[finding.variable_name for finding in _findings(source=source)])

    def test_helpers_and_public_keyword_contracts_have_no_blanket_exemption(self):
        source = (
            "class Codec:\n"
            "    def encode(self, *, value):\n"
            "        return value\n"
        )

        self.assertEqual(first=["value"], second=[finding.variable_name for finding in _findings(source=source)])

    def test_authored_python_bindings_explain_their_role(self):
        findings = [
            finding.diagnostic(source_path=source_path.relative_to(ROOT))
            for source_path in python_sources(production_only=False)
            for finding in _findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))

