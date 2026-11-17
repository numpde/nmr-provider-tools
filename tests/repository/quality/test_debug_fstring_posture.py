# Port source: https://github.com/numpde/magnet-deploy/blob/c178ea9b3612757d83a93dd207fdf0033fe54c79/repository_checks/test_structured_event_fact_posture.py
# Retain assignment checks; require debug syntax except for documented machine syntax.
"""Expose expression names in rendered facts, including values within prose.

Printed, logged and error messages use debug fields whose names read naturally.
Use ``# machine-string: <reason>`` only for strings that cannot meaningfully use
``{x=}``: a software consumer requires their path, protocol, command or data
syntax, and adding expression names would change that syntax. The reason must
identify the consumer or format and why expression names would be wrong.
Merely mentioning a path or URL in a human-readable message does not qualify.

Put the pragma immediately after the complete string literal, optionally after
its argument/item comma. For adjacent literals, put it after the last literal;
it covers their combined string. It does not cover another string inside a
replacement expression. Empty or unattached pragmas fail this check. This is
only an exemption from debug-field spelling: f-strings remain preferred over
other composition, and newline spelling and all other checks still apply.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import io
import re
import textwrap
import tokenize
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT, python_sources
from .fstrings import is_debug_expression as _is_debug_expression


_LITERAL_FIELD = re.compile(
    r"(?:^|[ ;])(?P<name>[^\W\d]\w*)="
    r"(?P<value>[a-zA-Z0-9][a-zA-Z0-9_.:-]*)(?=$|[ ;])"
)

_PERCENT_FIELD = re.compile(
    r"(?:^|[ ;])(?P<name>[^\W\d]\w*)="
    r"(?P<placeholder>%[-+0 #]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa])"
)

_MACHINE_STRING_PRAGMA = re.compile(r"# machine-string: (?P<reason>\S.*)")



@dataclass(frozen=True, slots=True, kw_only=True)
class _Finding:
    path: Path
    line: int
    expression: str
    shape: str

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.shape}: {self.expression!r}; "
            "render prose values with debug f-string syntax and names that read "
            "naturally; machine syntax requires a trailing # machine-string: <reason>"
        )




def _interpolation_findings(*, path: Path, source: str, joined: ast.JoinedStr) -> list[_Finding]:
    return [
        _Finding(
            path=path,
            line=value.value.lineno,
            expression=ast.unparse(ast_obj=value.value),
            shape="interpolation without debug syntax",
        )
        for value in joined.values
        if isinstance(value, ast.FormattedValue)
        and not _is_debug_expression(source=source, formatted=value)
    ]


def _constant_findings(*, path: Path, literal: ast.Constant) -> list[_Finding]:
    if not isinstance(literal.value, str):
        return []

    findings = [
        _Finding(
            path=path,
            line=literal.lineno + literal.value[: match.start()].count("\n"),
            expression=match.group("name"),
            shape=f"literal assignment to {match.group('value')!r}",
        )
        for match in _LITERAL_FIELD.finditer(string=literal.value)
    ]

    findings.extend(
        _Finding(
            path=path,
            line=literal.lineno + literal.value[: match.start()].count("\n"),
            expression=match.group("name"),
            shape=f"percent placeholder {match.group('placeholder')!r}",
        )
        for match in _PERCENT_FIELD.finditer(string=literal.value)
    )

    return findings


def _machine_string_exemptions(*, path: Path, source: str, literals: list[ast.AST]) -> tuple[set[ast.AST], list[_Finding]]:
    """Attach real trailing comments to complete literals, never to nearby code.

    Tokenizer columns count characters; AST columns count UTF-8 bytes. Compare
    byte positions so non-ASCII literals do not detach or widen an exemption.
    The checker verifies attachment; code review must verify the stated consumer.
    """
    literal_ends = {(literal.end_lineno, literal.end_col_offset): literal for literal in literals}

    # Unicode separators inside literals are data; only physical newlines
    # advance the tokenizer's source line number.
    source_lines = source.split("\n")

    exempt_literals = set()
    findings = []

    for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline):
        if token.type != tokenize.COMMENT or not token.string.startswith("# machine-string"):
            continue

        if _MACHINE_STRING_PRAGMA.fullmatch(string=token.string.rstrip()) is None:
            findings.append(_Finding(path=path, line=token.start[0], expression=token.string, shape="machine-string pragma requires a nonempty reason"))

            continue

        preceding = source_lines[token.start[0] - 1][:token.start[1]].rstrip()

        if preceding.endswith(","):
            preceding = preceding[:-1].rstrip()

        literal = literal_ends.get((token.start[0], len(preceding.encode(encoding="utf-8"))))

        if literal is None:
            findings.append(_Finding(path=path, line=token.start[0], expression=token.string, shape="machine-string pragma must immediately follow a complete string literal"))

            continue

        exempt_literals.add(literal)

    return (exempt_literals, findings)


def _findings(*, path: Path, source: str) -> list[_Finding]:
    tree = ast.parse(source=source, filename=str(path))

    # Dynamic widths and precisions control rendering; they are not facts in the
    # resulting text. Nested f-strings inside ordinary expressions still count.
    format_specs = {id(node.format_spec) for node in ast.walk(node=tree) if isinstance(node, ast.FormattedValue)}

    parents = {child: parent for parent in ast.walk(node=tree) for child in ast.iter_child_nodes(node=parent)}

    # Constants directly inside JoinedStr are synthesized string pieces,
    # not independent source literals. A pragma belongs to their complete f-string.
    literals = [
        node
        for node in ast.walk(node=tree)
        if (isinstance(node, ast.JoinedStr) and id(node) not in format_specs)
        or (isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(parents.get(node), ast.JoinedStr))
    ]

    (exempt_literals, findings) = _machine_string_exemptions(path=path, source=source, literals=literals)

    for node in ast.walk(node=tree):
        if node in exempt_literals or (isinstance(node, ast.Constant) and parents.get(node) in exempt_literals):
            continue

        if isinstance(node, ast.JoinedStr) and id(node) not in format_specs:
            findings.extend(_interpolation_findings(path=path, source=source, joined=node))
        elif isinstance(node, ast.Constant):
            findings.extend(_constant_findings(path=path, literal=node))

    return sorted(findings, key=lambda finding: (finding.line, finding.expression, finding.shape))


class DebugFstringPostureTests(unittest.TestCase):
    def test_machine_string_pragmas_are_explicit_and_attached_to_one_literal(self) -> None:
        cases = {
            "url.py.fixture": [],
            "resolver.py.fixture": [],
            "argument_comma.py.fixture": [],
            "adjacent.py.fixture": [],
            "multiline.py.fixture": [],
            "unicode.py.fixture": [],
            "unicode_separator.py.fixture": [],
            "constant.py.fixture": [],
            "printed.py.fixture": ["interpolation without debug syntax"],
            "logged.py.fixture": ["interpolation without debug syntax"],
            "error.py.fixture": ["interpolation without debug syntax"],
            "marker_in_string.py.fixture": ["interpolation without debug syntax"],
            "empty_reason.py.fixture": ["interpolation without debug syntax", "machine-string pragma requires a nonempty reason"],
            "whitespace_reason.py.fixture": ["interpolation without debug syntax", "machine-string pragma requires a nonempty reason"],
            "preceding.py.fixture": ["interpolation without debug syntax", "machine-string pragma must immediately follow a complete string literal"],
            "after_call.py.fixture": ["interpolation without debug syntax", "machine-string pragma must immediately follow a complete string literal"],
            "non_string.py.fixture": ["machine-string pragma must immediately follow a complete string literal"],
            "sibling.py.fixture": ["interpolation without debug syntax"],
            "nested.py.fixture": ["interpolation without debug syntax"],
            "early_adjacent.py.fixture": ["interpolation without debug syntax", "machine-string pragma must immediately follow a complete string literal"],
        }

        for (fixture_name, expected_shapes) in cases.items():
            with self.subTest(fixture_name=fixture_name):
                fixture_path = REPOSITORY_ROOT / "tests/repository/fixtures/quality/machine_strings" / fixture_name
                findings = _findings(path=fixture_path, source=fixture_path.read_text(encoding="utf-8"))

                self.assertEqual(first=sorted(expected_shapes), second=sorted(finding.shape for finding in findings))

    def test_raw_prose_values_and_manual_labels_are_rejected(self) -> None:
        expressions = (
            'f"Cannot open {label}."',
            'f"Cannot open {label!r}."',
            'f"label={label}"',
            'f"label={other!r}"',
            'f"label={(label)!r}"',
            'f"label={label:.2f}"',
            'f"{first}{second}"',
            'f"{label}" " was rejected."',
            '"µ" f"{\u03bb\u03ad\u03be\u03b7}"',
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                findings = _findings(path=Path("example.py"), source=f"message = {expression}\n")

                self.assertTrue(expr=findings)
                self.assertTrue(expr=all(finding.shape == "interpolation without debug syntax" for finding in findings))

    def test_debug_marker_is_independent_of_spacing_conversion_and_format(self) -> None:
        expressions = (
            'f"Cannot open {label = }."',
            'f"{label=}"',
            'f"{label = !s}"',
            'f"{label = !r}"',
            'f"{label = !a}"',
            'f"{duration = :.2f}"',
            'f"{duration = !r:>12}"',
            'f"{(duration) = :.2f}"',
            'f"{((duration)) = }"',
            'f"{duration = :{width}.{precision}f}"',
            'f"{error.stage = }"',
            '"µ" f"{\u03bb\u03ad\u03be\u03b7 = }"',
            'f"{label = }" " was rejected."',
            'f"{first = }" f"{second = }"',
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                self.assertEqual(first=[], second=_findings(path=Path("example.py"), source=f"message = {expression}\n"))

    def test_multiline_expressions_use_their_own_source_span(self) -> None:
        source = textwrap.dedent('''\
            debug = f"""{(
                duration
            ) = :.2f}"""
            manual = f"""duration={(
                duration
            ):.2f}"""
            debug_sum = f"""{(
                first +
                second
            ) = }"""
            ''')

        findings = _findings(path=Path("example.py"), source=source)

        self.assertEqual(first=[(5, "duration")], second=[(finding.line, finding.expression) for finding in findings])

    def test_dynamic_format_fields_are_structural_but_nested_fstrings_are_values(self) -> None:
        source = 'message = f"{duration:{width}.{precision}f}"\nmessage = f"{f\'{label}\' = }"\n'
        findings = _findings(path=Path("example.py"), source=source)

        self.assertEqual(first=["duration", "label"], second=[finding.expression for finding in findings])

    def test_literal_and_percent_assignments_retain_the_predecessor_census(self) -> None:
        source = 'message = "event=endpoint_failed; ready=true; count=%d; reason=%s"\n'
        findings = _findings(path=Path("example.py"), source=source)

        self.assertEqual(
            first=[
                ("count", "percent placeholder '%d'"),
                ("event", "literal assignment to 'endpoint_failed'"),
                ("ready", "literal assignment to 'true'"),
                ("reason", "percent placeholder '%s'"),
            ],
            second=[(finding.expression, finding.shape) for finding in findings],
        )

    def test_ordinary_data_formatting_is_reported_without_a_silent_exemption(self) -> None:
        source = 'path = f"/work/{name}.json"\ncommand = f"--port={port}"\nheader = "mode=live"\n'
        findings = _findings(path=Path("example.py"), source=source)

        self.assertEqual(first=["name", "port", "mode"], second=[finding.expression for finding in findings])

    def test_production_values_use_debug_fstring_syntax(self) -> None:
        findings = [
            finding
            for path in python_sources(production_only=True)
            for finding in _findings(path=path.relative_to(REPOSITORY_ROOT), source=path.read_text(encoding="utf-8"))
        ]

        if findings:
            self.fail(
                f"Production debug f-string census: {len(findings)} findings\n"
                + "\n".join(finding.diagnostic() for finding in findings)
            )
