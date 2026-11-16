"""Keep each newline escape at the end of its individual Python literal.

Split text across adjacent literals so source lines expose the rendered lines.
Only an actual backslash-n escape counts: raw spelling and an escaped backslash
are data, and physical newlines in triple-quoted strings already expose lines.
Each f-string owns its complete literal, including expressions and format specs;
a middle token boundary does not make an escape terminal. There are no exemptions.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import io
import tokenize
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT, python_sources


@dataclass(frozen=True, slots=True, kw_only=True)
class _Finding:
    path: Path
    line: int
    column: int

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}:{self.column}: "
            "newline escape must end its individual string literal; "
            "split the following text into an adjacent literal"
        )


def _offset(*, position: tuple[int, int], line_offsets: list[int]) -> int:
    (line, column) = position

    return line_offsets[line - 1] + column


def _escape_offsets(*, source: str, start: int, end: int) -> list[int]:
    """Read lexical escape pairs so an escaped backslash cannot start an escape."""

    offsets = []
    index = start

    while index < end:
        if source[index] != "\\":
            index += 1

            continue

        if index + 1 < end and source[index + 1] == "n":
            offsets.append(index)

        index += 2

    return offsets


def _findings(*, path: Path, source: str) -> list[_Finding]:
    line_offsets = [0]

    line_offsets.extend(index + 1 for (index, character) in enumerate(source) if character == "\n")

    # Token strings normalize doubled f-string braces. Their source coordinates
    # retain the original spelling, which owns both escape and diagnostic offsets.
    literals = []

    fstrings = []

    for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline):
        if token.type not in {tokenize.STRING, tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END}:
            continue

        start = _offset(position=token.start, line_offsets=line_offsets)
        end = _offset(position=token.end, line_offsets=line_offsets)

        if token.type in {tokenize.STRING, tokenize.FSTRING_START}:
            quote_start = next(index for (index, character) in enumerate(token.string) if character in {"'", '"'})
            prefix = token.string[:quote_start].lower()
            quote = token.string[quote_start]

            if token.string[quote_start:].startswith(quote * 3):
                quote_width = 3
            else:
                quote_width = 1

            if token.type == tokenize.FSTRING_START:
                fstrings.append(("r" in prefix, []))
            elif "r" not in prefix:
                body_end = end - quote_width
                escapes = _escape_offsets(source=source, start=start + quote_start + quote_width, end=body_end)

                literals.append((body_end, escapes))
        elif token.type == tokenize.FSTRING_MIDDLE:
            (raw, escapes) = fstrings[-1]

            if not raw:
                escapes.extend(_escape_offsets(source=source, start=start, end=end))
        elif token.type == tokenize.FSTRING_END:
            (raw, escapes) = fstrings.pop()

            literals.append((start, escapes))

    findings = []

    for (body_end, escapes) in literals:
        for offset in escapes:
            if offset + 2 == body_end:
                continue

            line = source.count("\n", 0, offset) + 1

            findings.append(_Finding(path=path, line=line, column=offset - line_offsets[line - 1] + 1))

    return sorted(findings, key=lambda finding: (finding.line, finding.column))


class EscapedNewlinePostureTests(unittest.TestCase):
    def test_source_fixtures_follow_individual_literal_boundaries(self) -> None:
        cases = {
            "accepted.py.fixture": [],
            "ordinary.py.fixture": [(1, 15), (2, 14), (3, 10), (4, 17), (5, 15), (6, 18)],
            "fstrings.py.fixture": [(1, 11), (2, 18), (3, 19), (4, 17), (5, 25), (6, 24), (7, 17)],
            "positions.py.fixture": [(1, 15), (2, 14), (5, 5), (7, 11), (8, 13), (9, 13)],
            "no_final_newline.py.fixture": [(1, 15)],
        }

        for (fixture_name, expected_positions) in cases.items():
            with self.subTest(fixture_name=fixture_name):
                path = REPOSITORY_ROOT / "tests/repository/fixtures/quality/escaped_newlines" / fixture_name
                findings = _findings(path=path, source=path.read_text(encoding="utf-8"))

                self.assertEqual(first=expected_positions, second=[(finding.line, finding.column) for finding in findings])

    def test_diagnostic_names_the_source_escape_column(self) -> None:
        path = REPOSITORY_ROOT / "tests/repository/fixtures/quality/escaped_newlines/positions.py.fixture"
        (finding, *remaining) = _findings(path=Path("positions.py.fixture"), source=path.read_text(encoding="utf-8"))

        self.assertEqual(
            first=(
                "positions.py.fixture:1:15: newline escape must end its individual "
                "string literal; split the following text into an adjacent literal"
            ),
            second=finding.diagnostic(),
        )

        self.assertEqual(first=5, second=len(remaining))

    def test_all_authored_python_ends_literals_at_newline_escapes(self) -> None:
        findings = [
            finding
            for path in python_sources(production_only=False)
            for finding in _findings(path=path.relative_to(REPOSITORY_ROOT), source=path.read_text(encoding="utf-8"))
        ]

        if findings:
            self.fail(
                f"Escaped newline census: {len(findings)=}\n"
                + "\n".join(finding.diagnostic() for finding in findings)
            )
