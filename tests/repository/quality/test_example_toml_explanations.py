"""Require nearby explanations for active and optional example TOML settings.

This checks presence, not prose quality. A comment belongs to the next setting
only; blank lines and table headers separate it from subsequent settings.
"""

from tools.container_guard import require_container

require_container()

import tomllib
import tempfile
import unittest
from pathlib import Path
from tomllib import TOMLDecodeError
from unittest.mock import patch

from . import sources


def _has_prose(comment):
    return any(character.isalpha() for character in comment)


def _optional_setting(comment):
    """Recognize a complete commented-out assignment, not prose containing '='."""
    if comment.startswith("["):
        return False
    try:
        return bool(tomllib.loads(comment))
    # silent-defaults: allow-exception-outcome -- Prose need not parse as TOML; syntax failure means this comment is not a complete optional assignment; proof=tests/repository/quality/test_example_toml_explanations.py::ExampleTomlExplanationTests.test_accepts_preceding_and_inline_comments_including_optional_settings
    except TOMLDecodeError:
        return False


def _inline_explanation(statement):
    # Parsing each prefix distinguishes a real trailing comment from '#' inside
    # quoted keys or values, including multiline strings and arrays.
    for offset, character in enumerate(statement):
        if character != "#":
            continue
        try:
            tomllib.loads(statement[:offset])
        except TOMLDecodeError:
            continue
        if _has_prose(statement[offset + 1:].splitlines()[0]):
            return True
    return False


def unexplained_settings(source):
    """Return line numbers lacking a preceding or inline explanation.

    Validate the complete document first. Parse complete statements so array
    elements, inline-table members and multiline string contents are not
    mistaken for independent setting lines.
    """
    tomllib.loads(source)
    lines = iter(enumerate(source.splitlines(), 1))
    explained = False
    findings = []
    for line_number, line in lines:
        stripped = line.strip()
        if not stripped:
            explained = False
            continue
        if stripped.startswith("#"):
            comment = stripped[1:].strip()
            if not _optional_setting(comment):
                explained = explained or _has_prose(comment)
                continue
            statement = comment
        else:
            statement = line
            while True:
                try:
                    tomllib.loads(statement)
                    break
                except TOMLDecodeError:
                    _, continuation = next(lines)
                    statement += "\n" + continuation
        if not statement.lstrip().startswith("["):
            if not explained and not _inline_explanation(statement):
                findings.append(line_number)
        explained = False
    return findings


class ExampleTomlExplanationTests(unittest.TestCase):
    def test_reports_each_unexplained_active_and_optional_setting(self):
        source = '# Header\n[api]\nfirst = 1\n# Why second exists.\nsecond = 2\nthird = 3\n\n# fourth = 4\n'
        self.assertEqual([3, 6, 8], unexplained_settings(source))

    def test_explanations_do_not_cross_blank_lines_or_count_separators(self):
        self.assertEqual([3, 5], unexplained_settings('# Explanation.\n\nx = 1\n# ---\ny = 2\n'))

    def test_accepts_preceding_and_inline_comments_including_optional_settings(self):
        source = '# Use x = one to select the first option.\nx = 1\ny = 2 # Why y exists.\n# Optional trust bundle.\n# ca_file = "/ca.pem"\n# z = 3 # Why z exists.\n'
        self.assertEqual([], unexplained_settings(source))

    def test_hashes_and_assignments_inside_values_are_not_explanations_or_settings(self):
        source = '"key#name" = "# not a comment"\n# An array of destinations.\naddresses = [\n "a=b", # Element comment.\n]\n# A multiline message.\nmessage = """\npretend = 1\n# Not documentation.\n"""\n'
        self.assertEqual([1], unexplained_settings(source))

    def test_nested_tables_dotted_keys_and_inline_tables(self):
        source = '[[bindings]]\n# Binding selector.\nname = "one"\n[bindings.api]\n"quoted=key" = 1\n# Nested configuration.\noptions = { retry = 1, delay = 2 }\nother.value = 3\n'
        self.assertEqual([5, 8], unexplained_settings(source))

    def test_invalid_toml_is_not_silently_skipped(self):
        with self.assertRaises(TOMLDecodeError):
            unexplained_settings('broken = [\n')

    def test_new_nested_example_files_are_checked_with_file_and_line_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "nested/new.example.toml"
            example.parent.mkdir()
            example.write_text('unexplained = 1\n', encoding="utf-8")
            with patch.object(sources, "ROOT", root):
                with self.assertRaisesRegex(AssertionError, r"nested/new\.example\.toml:1:"):
                    self.test_all_example_toml_settings_have_explanations()

    def test_all_example_toml_settings_have_explanations(self):
        findings = []
        for path in sorted(sources.ROOT.rglob("*.example.toml")):
            if sources.is_skipped(path):
                continue
            for line in unexplained_settings(path.read_text(encoding="utf-8")):
                findings.append(
                    f"{path.relative_to(sources.ROOT)}:{line}: explain this setting in a comment "
                    "directly above it or on the same line"
                )
        self.assertEqual([], findings, msg="\n".join(findings))
