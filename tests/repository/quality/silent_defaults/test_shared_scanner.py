# Port source: https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/silent_defaults/test_shared_scanner.py
# Original detector semantics and fixtures retained; inventories use authored NMRNet paths.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import unittest
from pathlib import Path

from ..sources import ROOT, is_skipped

SELF_PARTS = ("tests", "repository", "quality", "silent_defaults")
SCANNER_TEST_MARKER = "active"
# SCANNER_TEST_MARKER = "comment"


def is_literal_make_origin_gate(source: str, line: str) -> bool:
    """Recognize one complete Make assignment that forwards owned literal input.

    The local Makefile's gate denies ambient values; an absent command-line input
    stays absent. It selects no replacement. Keep broad raw matchers unchanged
    and classify only this complete form at the production finding boundary.
    No trailing text, alternate origin, else branch or nested value is admitted.
    Whitespace is flexible only around assignment tokens; Make can preserve
    whitespace inside arguments or after the value as literal payload bytes.
    https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L74-L78
    """
    path = Path(source)
    if path.name != "Makefile" and path.suffix != ".mk":
        return False
    return (
        re.fullmatch(
            r"[ ]*(?:override[ \t]+)?export[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*:=[ \t]*"
            r"\$\(if \$\(filter command line,\$\(origin (?P<variable>[A-Za-z_][A-Za-z0-9_]*)\)\),"
            r"\$\(value (?P=variable)\)\)",
            line,
        )
        is not None
    )


def repo_files(patterns: tuple[str, ...]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(ROOT.glob(pattern))
    return sorted(path for path in paths if path.is_file() and not excluded(path))


def excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return is_skipped(path) or relative.parts[: len(SELF_PARTS)] == SELF_PARTS


def line_findings(
    paths: list[Path],
    matcher: re.Pattern[str],
    label: str,
    *,
    skip_comments: bool = True,
) -> list[str]:
    findings: list[str] = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if skip_comments and line.lstrip().startswith("#"):
                continue
            for match in matcher.finditer(line):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: {label}: {match.group(0).strip()}")
    return findings


class SharedScannerTest(unittest.TestCase):
    def test_repo_files_excludes_this_check_package(self) -> None:
        self.assertEqual([], repo_files(("tests/repository/quality/silent_defaults/*.py",)))

    def test_repo_files_excludes_skipped_dependency_material(self) -> None:
        self.assertEqual([], repo_files(("NMRNetplusplus/node_modules/**/*.ts",)))

    def test_line_findings_reports_relative_paths_and_skips_comments(self) -> None:
        path = Path(__file__).resolve()
        findings = line_findings([path], re.compile(r"^SCANNER_TEST_MARKER = "), "marker")
        marker_line = next(
            line_number
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith("SCANNER_TEST_MARKER = ")
        )

        self.assertEqual(
            [f"tests/repository/quality/silent_defaults/test_shared_scanner.py:{marker_line}: marker: SCANNER_TEST_MARKER ="],
            findings,
        )


if __name__ == "__main__":
    unittest.main()
