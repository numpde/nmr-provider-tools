"""Prove exact image deletion respects engine borrowers and current selections."""
from tools.container_guard import require_container
require_container()

from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from host.tools.image_retirement import RetirementRefused, retire, select_images, retained_image_ids


CURRENT = ("a" * 64, "b" * 64)
OLD = "c" * 64
IDENTIFIER = "sha256:" + "d" * 64
NOW = 1800000000


def image(**changes):
    value = {"Id": IDENTIFIER, "Labels": {"org.nmr-provider-tools.inputs": OLD},
             "RepoTags": ["nmr-provider-tools/checker:" + OLD], "RepoDigests": [],
             "Created": "2020-01-01T00:00:00Z"}
    return value | changes


class Engine:
    # silent-defaults: allow-contract -- The synthetic Docker engine defaults to unborrowed, unmodified, well-formed metadata; each adversarial test opts in explicitly.
    def __init__(self, value, *, borrower=False, changed=False, malformed=False):
        self.value = value
        self.borrower, self.changed, self.malformed = borrower, changed, malformed
        self.calls = []
        self.inspections = 0

    def __call__(self, *arguments):
        self.calls.append(arguments)
        if arguments[:2] == ("ps", "--all"):
            if self.borrower:
                return "1" * 64 + "\n"
            return ""
        if arguments[:3] == ("inspect", "--type", "container"):
            return json.dumps(IDENTIFIER) + "\n"
        if arguments[:2] == ("image", "ls"):
            return IDENTIFIER + "\n"
        if arguments[:2] == ("image", "inspect"):
            self.inspections += 1
            value = deepcopy(self.value)
            if self.changed and self.inspections > 1:
                value["RepoTags"] = ["somebody-else/retained:latest"]
            if self.malformed:
                return "{broken"
            return json.dumps(value) + "\n"
        if arguments == ("image", "rm", "--no-prune", IDENTIFIER):
            return ""
        raise AssertionError(arguments)

    @property
    def removals(self):
        return [call for call in self.calls if call[:2] == ("image", "rm")]


class ImageRetirementTests(unittest.TestCase):
    def test_legacy_delivery_metadata_preserves_only_that_image(self):
        legacy = image(Id="sha256:" + "e" * 64,
                       Labels={"org.nmr-provider-tools.role": "delivery"}, RepoTags=[])
        self.assertEqual([IDENTIFIER], select_images([legacy, image()], set(), CURRENT, NOW))

    def test_native_creation_time_accepts_explicit_offset(self):
        value = image(Created="2020-01-01T03:00:00.123456789+03:00")
        self.assertEqual([IDENTIFIER], select_images([value], set(), CURRENT, NOW))

    def test_only_exact_obsolete_owned_id_is_removed(self):
        engine = Engine(image())
        with tempfile.TemporaryDirectory() as directory:
            count = retire(Path(directory), engine, lambda: CURRENT, now=NOW, operations=Path(directory))
        self.assertEqual(count, 1)
        self.assertEqual(engine.removals, [("image", "rm", "--no-prune", IDENTIFIER)])

    def test_container_borrower_or_staged_id_is_protected(self):
        for borrowed, staged in ((True, ()), (False, (IDENTIFIER,))):
            with self.subTest(borrowed=borrowed), tempfile.TemporaryDirectory() as directory:
                engine = Engine(image(), borrower=borrowed)
                self.assertEqual(0, retire(Path(directory), engine, lambda: CURRENT, staged=staged, now=NOW, operations=Path(directory)))
                self.assertEqual([], engine.removals)

    def test_changed_alias_or_new_container_prevents_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(image(), changed=True)
            self.assertEqual(0, retire(Path(directory), engine, lambda: CURRENT, now=NOW, operations=Path(directory)))
            self.assertEqual([], engine.removals)
        engine = Engine(image())
        original = engine.__call__
        def borrowed_later(*arguments):
            if "--filter" in arguments and arguments[-1].startswith("ancestor="):
                return "2" * 64 + "\n"
            return original(*arguments)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(0, retire(Path(directory), borrowed_later, lambda: CURRENT, now=NOW, operations=Path(directory)))
            self.assertEqual([], engine.removals)

    def test_current_inputs_and_unknown_aliases_and_recent_outputs_remain(self):
        cases = [
            image(Labels={"org.nmr-provider-tools.inputs": CURRENT[0]},
                  RepoTags=["nmr-provider-tools/checker:" + CURRENT[0]]),
            image(RepoTags=["foreign/retained:latest"]),
            image(RepoTags=["nmr-provider-tools/base-operation:held"]),
            image(RepoDigests=["foreign/retained@sha256:" + "f" * 64]),
            image(RepoTags=["nmr-provider-tools/checker:" + OLD, "foreign/retained:latest"]),
            image(RepoTags=[], Created=datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat().replace("+00:00", "Z")),
            image(Labels={"org.nmr-provider-tools.role": "delivery",
                          "org.nmr-provider-tools.inputs": OLD,
                          "org.nmr-provider-tools.recipe": CURRENT[1]}, RepoTags=[]),
            image(Labels={}, RepoTags=["nmr-provider-tools/wheelhouse-tool:local"]),
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual([], select_images([value], set(), CURRENT, NOW))
        self.assertEqual(select_images([image(Labels={"org.nmr-provider-tools.role": "wheelhouse-tool"},
                                             RepoTags=[])], set(), CURRENT, NOW), [IDENTIFIER])

    def test_incomplete_metadata_fails_before_any_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(image(), malformed=True)
            with self.assertRaises(ValueError):
                retire(Path(directory), engine, lambda: CURRENT, now=NOW, operations=Path(directory))
            self.assertEqual([], engine.removals)

    def test_current_source_change_fails_before_removal(self):
        identities = iter((CURRENT, ("e" * 64, CURRENT[1])))
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(image())
            with self.assertRaises(RetirementRefused):
                retire(Path(directory), engine, lambda: next(identities), now=NOW, operations=Path(directory))
            self.assertEqual([], engine.removals)

    def test_known_retained_build_receipt_protects_exact_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operation = root / "nmr-provider-tools.abcdefgh"
            operation.mkdir()
            receipt = operation / "built-image"
            receipt.write_text(IDENTIFIER + "\n")
            engine = Engine(image())
            self.assertEqual(0, retire(root, engine, lambda: CURRENT, now=NOW, operations=root))
            self.assertEqual([], engine.removals)
            self.assertEqual(retained_image_ids(root), {IDENTIFIER})
            receipt.unlink()
            self.assertEqual(retained_image_ids(root), set())
            receipt.write_text("partial")
            self.assertEqual(retained_image_ids(root), set())
            receipt.write_text(IDENTIFIER + "\n\n")
            self.assertEqual(retained_image_ids(root), set())
            unrelated = root / "other-owner"
            unrelated.mkdir()
            (unrelated / "built-image").write_text(IDENTIFIER)
            self.assertEqual(retained_image_ids(root), set())
