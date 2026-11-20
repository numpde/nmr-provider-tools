"""Flag vague status words separately in code and other public text.

The words can have precise meanings, including established protocol states;
each finding needs review rather than an automatic replacement. The checker
itself must name the words and provide examples, so it is excluded from its
own scan. The shared source inventory excludes private files and Python fixture
samples; release artifacts are not mounted into the repository checker.
"""

from tools.container_guard import require_container

require_container()

import pathlib
import re
import unittest

from operations.checker.findings import assert_no_findings

from .sources import ROOT, source_files


# repo-check: allow-module-constant
_WORDS = frozenset(
    {
        "admit",
        "admits",
        "admitted",
        "admitting",
        "admission",
        "admissions",
        "qualify",
        "qualifies",
        "qualified",
        "qualifying",
        "qualification",
        "qualifications",
        "approve",
        "approves",
        "approved",
        "approving",
        "approval",
        "approvals",
    }
)

# repo-check: allow-module-constant
_WORD_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+")

# repo-check: allow-module-constant
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".sh",
        ".bash",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".css",
        ".go",
        ".rs",
        ".sql",
        ".awk",
        ".pl",
        ".rb",
        ".mk",
    }
)

# repo-check: allow-module-constant
_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".html",
        ".toml",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".csv",
        ".example",
        ".rst",
        ".lock",
        ".svg",
    }
)


def _word_findings(*, source_text: str) -> list[tuple[int, str]]:
    findings = []

    for (line_number, source_line) in enumerate(source_text.splitlines(), start=1):
        for word in _WORD_PARTS.finditer(string=source_line):
            if word.group().casefold() in _WORDS:
                findings.append((line_number, word.group()))

    return findings


def _is_code(*, source_path: pathlib.Path, source_bytes: bytes) -> bool:
    return (
        (source_path.suffix.casefold() in _CODE_SUFFIXES) or
        (source_path.name in {"Makefile", "Dockerfile", "Dockerfile.lock"}) or
        source_bytes.startswith(b"#!")
    )


def _text_findings(*, code: bool) -> list[str]:
    public_files = source_files(root=ROOT, patterns=("**/*",), exclude_parts=frozenset({".idea", ".vscode"}))
    bootstrap_files = source_files(root=ROOT / "bootstrap", patterns=("**/*",))
    findings = []

    for source_path in (*public_files, *bootstrap_files):
        if source_path == pathlib.Path(__file__):
            continue

        # Local deployment files are ignored by Git; only their examples are public.
        if source_path.is_relative_to(ROOT / "config/deployments"):
            if not source_path.name.endswith(".toml.example"):
                continue

        source_bytes = source_path.read_bytes()
        is_code = _is_code(source_path=source_path, source_bytes=source_bytes)

        if is_code != code:
            continue

        known_text = is_code or (source_path.suffix.casefold() in _TEXT_SUFFIXES)

        if b"\0" in source_bytes:
            if known_text:
                findings.append(f"Cannot scan text containing null bytes: {source_path=}")

            continue

        try:
            source_text = source_bytes.decode(encoding="utf-8")
        except UnicodeDecodeError as error:
            if known_text:
                findings.append(f"Cannot scan text as UTF-8: {source_path=}; {error=!s}")

            # Other non-text artifacts have no prose or identifier names to review.
            continue

        for (line_number, word) in _word_findings(source_text=source_text):
            findings.append(f"{source_path.relative_to(ROOT)}:{line_number}: review {word=!r}")

    return findings


class WeaselWordPostureTests(unittest.TestCase):
    def test_words_are_found_in_prose_and_identifiers(self):
        source_text = (
            "admitted = True\n"
            "class NotAdmitted: pass\n"
            "qualified_release = False\n"
            "# Awaiting approval.\n"
            "disqualified = True\n"
            "APPROVED = 1\n"
            "class AdmissionCode: pass\n"
        )

        self.assertEqual(
            first=[(1, "admitted"), (2, "Admitted"), (3, "qualified"), (4, "approval"), (6, "APPROVED"), (7, "Admission")],
            second=_word_findings(source_text=source_text),
        )

    def test_code_and_other_files_are_separate(self):
        self.assertTrue(expr=_is_code(source_path=pathlib.Path("service.py"), source_bytes=b""))
        self.assertTrue(expr=_is_code(source_path=pathlib.Path("launcher"), source_bytes=b"#!/bin/sh\n"))
        self.assertTrue(expr=_is_code(source_path=pathlib.Path("Dockerfile.lock"), source_bytes=b""))
        self.assertFalse(expr=_is_code(source_path=pathlib.Path("deployment.toml"), source_bytes=b""))
        self.assertFalse(expr=_is_code(source_path=pathlib.Path("notes.html"), source_bytes=b""))

    def test_code_words_are_reviewed(self):
        findings = _text_findings(code=True)

        assert_no_findings(findings)

    def test_other_text_words_are_reviewed(self):
        findings = _text_findings(code=False)

        assert_no_findings(findings)
