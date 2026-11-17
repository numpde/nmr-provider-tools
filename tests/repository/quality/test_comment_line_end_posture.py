"""Keep a word attached to the phrase it starts in standalone comments."""

from tools.container_guard import require_container

require_container()

import io
import re
import tokenize
import unittest

from .sources import ROOT, python_sources


def _hanging_words(*, source: str) -> list[int]:
    source_lines = source.splitlines()

    comments = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and not source_lines[token.start[0] - 1][:token.start[1]].strip()
    ]

    findings = []

    for (comment, following_comment) in zip(comments, comments[1:]):
        if following_comment.start != (comment.start[0] + 1, comment.start[1]):
            continue

        trailing_word = re.search(r"([,;:.!?])\s+([A-Za-z]+)$", comment.string)

        if trailing_word is None:
            continue

        if trailing_word.group(1) == "," and trailing_word.group(2).casefold() in {"and", "or", "but"}:
            continue

        findings.append(comment.start[0])

    return findings


class CommentLineEndPostureTests(unittest.TestCase):
    def test_hanging_word_before_continuation_is_flagged(self):
        source = (
            "# The interpreter accepts the proposal; the\n"
            "# preparation tool validates it.\n"
        )

        self.assertEqual(first=[1], second=_hanging_words(source=source))

    def test_conjunctions_after_comma_are_allowed(self):
        for conjunction in ("and", "or", "but"):
            with self.subTest(conjunction=conjunction):
                source = (
                    f"# The first option is available, {conjunction}\n"
                    f"# the second option may also apply.\n"
                )

                self.assertEqual(first=[], second=_hanging_words(source=source))

    def test_unrelated_comments_and_strings_are_not_continuations(self):
        source = (
            "# Heading: tools\n"
            "\n"
            "# A separate comment.\n"
            "text = '# Not a comment; the'\n"
            "call()  # Inline note: this\n"
            "# Standalone comment.\n"
        )

        self.assertEqual(first=[], second=_hanging_words(source=source))

    def test_authored_comments_have_no_hanging_words(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line_number}: move the trailing word to its phrase"
            for source_path in python_sources(production_only=False)
            for line_number in _hanging_words(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
