"""Put each method of a multiline call chain on its own line."""

from tools.container_guard import require_container

require_container()

import ast
import re
import unittest

from .sources import ROOT, python_sources


def _findings(*, source: str) -> list[int]:
    source_lines = source.splitlines()
    findings = set()

    for expression in ast.walk(node=ast.parse(source=source)):
        if (not isinstance(expression, ast.Call)) or (expression.lineno == expression.end_lineno):
            continue

        methods = []
        current = expression

        while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            methods.append(current.func)
            current = current.func.value

        if len(methods) < 2:
            continue

        for method in methods:
            method_line = source_lines[method.end_lineno - 1]
            method_prefix = method_line.encode(encoding="utf-8")[:method.end_col_offset].decode(encoding="utf-8")
            method_suffix = re.search(pattern=rf"\.\s*{re.escape(method.attr)}$", string=method_prefix)

            if (method_suffix is not None) and (method_prefix[:method_suffix.start()].strip()):
                findings.add(method.end_lineno)

    return sorted(findings)


class MultilineMethodChainPostureTests(unittest.TestCase):
    def test_wrapped_chain_keeps_each_method_on_its_own_line(self):
        source = (
            "prompt = (\n"
            "    prompt_files.joinpath(\n"
            "        prompt_name,\n"
            "    ).read_text(encoding='utf-8')\n"
            ")\n"
        )

        self.assertEqual(first=[2, 4], second=_findings(source=source))

    def test_one_method_per_line_is_accepted(self):
        source = (
            "prompt = (\n"
            "    prompt_files\n"
            "    .joinpath(prompt_name)\n"
            "    .read_text(encoding='utf-8')\n"
            ")\n"
        )

        self.assertEqual(first=[], second=_findings(source=source))

    def test_single_line_chain_and_single_multiline_call_are_accepted(self):
        source = (
            "text = prompt_files.joinpath(prompt_name).read_text()\n"
            "report = make_report(\n"
            "    candidate,\n"
            ")\n"
        )

        self.assertEqual(first=[], second=_findings(source=source))

    def test_authored_python_uses_multiline_method_chain_layout(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line_number}: start each method of a multiline chain on its own line"
            for source_path in python_sources(production_only=False)
            for line_number in _findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
