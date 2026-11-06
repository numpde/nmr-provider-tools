"""Keep each versioned release-note paragraph and list item on one source line."""

from tools.container_guard import require_container

require_container()

import re
import tempfile
import unittest
from pathlib import Path

from . import sources


_GUIDANCE = "Keep each paragraph or list item on one source line; remove hard wrapping."
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_SEPARATOR = re.compile(r"^ {0,3}(?:[-*_][ \t]*){3,}$|^ {0,3}=+[ \t]*$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-+*]|[0-9]{1,9}[.)])[ \t]+\\S")


def wrapped_lines(source: str) -> list[int]:
    """Locate continuations, excluding headings, separate items and fenced code."""
    findings = []
    previous = None
    fence = None

    for number, line in enumerate(source.splitlines(), 1):
        if fence is not None:
            marker = fence[0]
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue

        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            previous = None
        elif not line.strip() or _HEADING.match(line) or _SEPARATOR.match(line):
            previous = None
        elif _LIST_ITEM.match(line):
            previous = "item"
        else:
            if previous is not None:
                findings.append(number)
            previous = "paragraph"

    return findings


def scan_release_notes(root: Path) -> list[str]:
    """Return file:line diagnostics for every matching release note."""
    return [
        f"{path.relative_to(root).as_posix()}:{number}: {_GUIDANCE}"
        for path in sorted((root / "notes").glob("*_release_v*.md"))
        for number in wrapped_lines(path.read_text(encoding="utf-8"))
    ]


class ReleaseNotesTests(unittest.TestCase):
    def test_detects_each_wrapped_paragraph_and_list_item(self):
        source = (
            "A paragraph starts here\n"
            "and continues here\n"
            "and here\n"
            "\n"
            "- First item\n"
            "  continuation of item\n"
            "- Second item\n"
            "1. Numbered item\n"
            "   continued numbered item\n"
        )
        self.assertEqual([2, 3, 6, 9], wrapped_lines(source))

    def test_allows_blank_lines_headings_separate_items_and_fenced_code(self):
        source = (
            "# Release heading\n\n"
            "One complete paragraph.\n\n"
            "- Item one\n"
            "  - Nested item\n"
            "2. Numbered item\n"
            "\n### New section\n"
            "\n```python\n"
            "Multi-line code\n"
            "two more lines\n"
            "- Not a Markdown item\n"
            "```\n"
            "\n~~~text\n"
            "Another multi-line example\n"
            "~~~\n"
            "\n---\n"
            "After the separator.\n"
        )
        self.assertEqual([], wrapped_lines(source))

    def test_discovers_new_versioned_notes_and_reports_file_and_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes").mkdir()
            (root / "notes/new_release_v9.8.7.md").write_text(
                "# Release\n\nFirst line\nwrapped line\n",
                encoding="utf-8",
            )
            (root / "notes/other.md").write_text("not\nscoped\n", encoding="utf-8")
            self.assertEqual(
                [f"notes/new_release_v9.8.7.md:4: {_GUIDANCE}"],
                scan_release_notes(root),
            )

    def test_all_versioned_release_notes_have_one_line_blocks(self):
        findings = scan_release_notes(sources.ROOT)
        self.assertEqual([], findings, msg="\n".join(findings))
