"""Use consistent f-string prefixes within implicit string concatenation."""

from tools.container_guard import require_container

require_container()

import io
import tokenize
import unittest

from .sources import ROOT, python_sources


def mixed_string_prefix_lines(*, source: str) -> list[int]:
    """Report the first mixed prefix in each run of adjacent literals."""
    # Each f-string gets its own context so literals in replacement expressions
    # cannot affect concatenation around the enclosing f-string
    runs = [None]
    findings = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.STRING, tokenize.FSTRING_START):
            formatted = token.type == tokenize.FSTRING_START
            previous = runs[-1]

            if previous is None:
                runs[-1] = (formatted, False)
            elif formatted != previous[0] and not previous[1]:
                findings.append(token.start[0])
                runs[-1] = (previous[0], True)

            if formatted:
                runs.append(None)
        elif token.type == tokenize.FSTRING_END:
            runs.pop()
        elif token.type not in (tokenize.NL, tokenize.COMMENT):
            runs[-1] = None

    return findings


class StringConcatenationPostureTests(unittest.TestCase):
    def test_reports_mixed_prefixes_across_lines_and_comments(self):
        source = 'message = (\n    "Expected more "  # context\n    f"for {value=}"\n    " here"\n)\n'

        self.assertEqual(first=[3], second=mixed_string_prefix_lines(source=source))

    def test_accepts_consistent_prefixes_and_separate_expressions(self):
        source = """a = "one" "two"
b = f"one" rf"{value}"
c = "one" + f"{value}"
d = f"{'one' 'two'}"
"""

        self.assertEqual(first=[], second=mixed_string_prefix_lines(source=source))

    def test_nested_replacement_expression_has_its_own_run(self):
        source = """a = f"{'one' f'{value}'}" f"two"
"""

        self.assertEqual(first=[1], second=mixed_string_prefix_lines(source=source))

    def test_authored_python_uses_consistent_concatenated_prefixes(self):
        findings = [
            f"{path.relative_to(ROOT)}:{line}: use either all f-strings or all plain strings in implicit concatenation"
            for path in python_sources(production_only=False)
            for line in mixed_string_prefix_lines(source=path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
