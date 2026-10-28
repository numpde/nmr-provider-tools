"""Keep copied check coverage and explicit donor exclusions independently visible."""

from tools.container_guard import require_container

require_container()

import ast
import json
import re
import unittest

from tests.repository.quality.sources import ROOT


class PortInventoryTests(unittest.TestCase):
    def test_every_copied_test_method_is_present_or_explicitly_disposed(self):
        manifest = json.loads((ROOT / "tests/repository/port-sources.json").read_text())
        dispositions = json.loads((ROOT / "tests/repository/port-dispositions.json").read_text())
        targets = [entry["target"] for entry in manifest["files"]]
        self.assertEqual(len(targets), len(set(targets)), msg="Copied targets must not have duplicate source authorities.")
        for entry in manifest["files"]:
            target = ROOT / entry["target"]
            with self.subTest(target=entry["target"]):
                self.assertTrue(target.is_file() and not target.is_symlink())
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                if not target.name.endswith(".py"):
                    continue
                tree = ast.parse(target.read_text())
                present = {
                    f"{owner.name}.{method.name}"
                    for owner in ast.walk(tree)
                    if isinstance(owner, ast.ClassDef)
                    for method in owner.body
                    if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
                }
                excluded = {item["method"] for item in dispositions if item["target"] == entry["target"]}
                expected = set(entry["test_methods"])
                missing = {name for name in expected if name.split(".")[-1] in excluded}
                # New owner tests may extend a port; original coverage still
                # requires preservation or its exact explicit disposition.
                self.assertLessEqual(expected, present | missing)
                self.assertEqual(set(), present & missing, msg="A disposition cannot silently hide a still-present test.")
                self.assertEqual(excluded, {name.split(".")[-1] for name in missing})

    def test_donor_module_classification_covers_each_copied_module_once(self):
        manifest = json.loads((ROOT / "tests/repository/port-sources.json").read_text())
        inventory = json.loads((ROOT / "tests/repository/donor-modules.json").read_text())
        self.assertEqual(manifest["commit"], inventory["commit"])
        modules = inventory["modules"]
        names = [entry["source"] for entry in modules]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            {
                entry["source"]
                for entry in manifest["files"]
                if entry["source"].startswith("repository_checks/") and entry["source"].endswith(".py")
            },
            {entry["source"] for entry in modules if entry["disposition"] == "ported"},
        )
        for entry in modules:
            self.assertTrue(entry["reason"].strip())
            self.assertIn(entry["disposition"], {"ported", "provider-owned", "on-owner-extraction", "no-current-consumer"})

    def test_checker_identity_is_one_exact_immutable_image_input(self):
        path = ROOT / "tools/base_image.sh"
        self.assertFalse(path.is_symlink())
        self.assertEqual(1, len(re.findall(rb"python:3\.12\.[0-9]+-slim-bookworm@sha256:[0-9a-f]{64}", path.read_bytes())))
        self.assertFalse((ROOT / "config/checker-image.txt").exists())
