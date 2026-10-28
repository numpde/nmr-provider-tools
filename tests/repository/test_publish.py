"""Publication preserves authored notes, inspected routing and qualified bytes.

Ports the release-only cases from pay-to-play-wire 4c92a3b test_publish.py.
Fixture Git/GitHub/Make tools model host effects; real installed-artifact proof
belongs to tests/package, which qualified the issued release independently.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tools/publish.sh", "tools/inference_native.sh", "tools/wheelhouse_path.sh"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(name, target)
        self.note = self.root / "notes/005_release_v0.2.0.md"
        self.note.parent.mkdir()
        self.note.write_text("# Authored fixture release\n\nThe reader-facing change.\n")
        self.inputs = self.root / "inputs"
        self.inputs.mkdir(mode=0o700)
        artifacts = self.inputs / "artifacts"
        artifacts.mkdir()
        contents = {
            "provider_core-0.2.0-py3-none-any.whl": b"qualified wheel",
            "provider_core-0.2.0.tar.gz": b"qualified source",
            "provider_core-0.2.0-install.lock": b"qualified lock",
            "provider_core-0.2.0-native-install.lock": b"qualified native lock",
            "provider_core-0.2.0-release.json": json.dumps({"source_revision": "0" * 39 + "1"}).encode(),
            "provider_core-0.2.0-sha256.json": b"{}",
        }
        for name, content in contents.items():
            (artifacts / name).write_bytes(content)
        self.receipts = "".join(f"QUALIFIED_SHA256 {hashlib.sha256(content).hexdigest()} {name}\n" for name, content in contents.items())
        (self.inputs / "release-test.log").write_text(self.receipts)
        (self.inputs / "release-build.log").write_text("qualified build\n")
        shutil.copyfile(self.note, self.inputs / "release-notes.md")
        self.trace = self.root / "calls"
        self.bin = Path("tests/fixtures/publish-tools").resolve()

    def run_publisher(self, scenario, action):
        return subprocess.run(
            ["bash", str(self.root / "tools/publish.sh"), action], cwd=self.root,
            env={"PATH": str(self.bin) + os.pathsep + os.defpath, "TMPDIR": str(self.root),
                 "TAG_INPUT_ORIGIN": "command line", "TAG_INPUT": "v0.2.0",
                 "ARTIFACTS_INPUT_ORIGIN": "command line", "ARTIFACTS_INPUT": str(self.inputs),
                 "OUTPUT_INPUT_ORIGIN": "command line", "OUTPUT_INPUT": str(self.root / "prepared"),
                 "WHEELHOUSE_INPUT_ORIGIN": "command line", "WHEELHOUSE_INPUT": str(self.root / "pool"),
                 "PUBLISH_TEST_MODE": scenario, "PUBLISH_TEST_TRACE": str(self.trace)},
            capture_output=True, text=True, timeout=15, check=False,
        )

    def test_prepare_keeps_authored_note_and_qualified_files_without_github_access(self):
        (self.root / "prepared").mkdir(mode=0o700)
        (self.root / "pool").mkdir()
        result = self.run_publisher("publish", "prepare")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(self.note.read_bytes(), (self.root / "prepared/release-notes.md").read_bytes())
        self.assertEqual(6, len(list((self.root / "prepared/artifacts").iterdir())))
        self.assertNotIn("gh ", self.trace.read_text())

    def test_matching_url_rewrite_or_unreadable_configuration_stops_before_github(self):
        for scenario in ("matching", "config-error"):
            with self.subTest(scenario=scenario):
                self.trace.unlink(missing_ok=True)
                result = self.run_publisher(scenario, "release")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                calls = self.trace.read_text()
                self.assertRegex(result.stderr, "Git URL rewrite|Git URL routing")
                self.assertNotIn("gh ", calls)
                self.assertNotIn("git push", calls)

    def test_existing_release_and_uncertain_api_result_never_push(self):
        for scenario in ("existing", "api-error"):
            with self.subTest(scenario=scenario):
                self.trace.unlink(missing_ok=True)
                result = self.run_publisher(scenario, "release")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                calls = self.trace.read_text()
                self.assertIn("gh api --hostname github.com --include", calls)
                self.assertNotIn("refs/tags/v0.2.0:refs/tags/v0.2.0", calls)
                self.assertNotIn("gh release create", calls)

    def test_duplicate_malformed_or_incomplete_receipts_never_reach_github(self):
        for receipts in (self.receipts + self.receipts.splitlines()[0] + "\n", self.receipts.replace("QUALIFIED_SHA256 ", "QUALIFIED_SHA256 broken ", 1), ""):
            with self.subTest(receipts=receipts):
                (self.inputs / "release-test.log").write_text(receipts)
                self.trace.unlink(missing_ok=True)
                result = self.run_publisher("publish", "release")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertRegex(result.stderr, "artifact receipt|artifact name|all six artifact hashes")
                self.assertNotIn("gh ", self.trace.read_text())

    def test_downloaded_artifact_change_leaves_the_release_draft(self):
        result = self.run_publisher("corrupt", "release")
        self.assertEqual(2, result.returncode, msg=result.stderr)
        calls = self.trace.read_text()
        self.assertIn("gh release download", calls)
        self.assertNotIn("gh release edit", calls)

    def test_only_byte_verified_assets_are_made_public(self):
        result = self.run_publisher("publish", "release")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        calls = self.trace.read_text()
        self.assertLess(calls.index("gh release download"), calls.index("gh release edit"))
        self.assertEqual(8, len(list(Path(str(self.trace) + ".uploads").iterdir())))
        self.assertIn("--draft=false", calls)

    def test_unknown_native_retirement_stops_before_any_tag_or_draft_effect(self):
        (self.root / "tools/inference_native.sh").write_text("""inference_native() {
    local operation=$1 status=0
    shift 3
    if ! "$inference_retirement_confirmed"; then
        printf 'continued after unknown retirement\\n' >> "$PUBLISH_TEST_TRACE"
        return 97
    fi
    "$@" || status=$?
    if [[ $operation == 'existing GitHub release inspection' ]]; then
        inference_retirement_confirmed=false
        return 1
    fi
    return "$status"
}
""")
        result = self.run_publisher("publish", "release")
        self.assertEqual(1, result.returncode, msg=result.stderr)
        calls = self.trace.read_text()
        self.assertIn("gh api --hostname github.com --include", calls)
        self.assertNotIn("continued after unknown retirement", calls)
        self.assertNotIn("gh release create", calls)
        self.assertNotIn("refs/tags/v0.2.0:refs/tags/v0.2.0", calls)

    def test_failed_git_status_cannot_be_treated_as_clean_source(self):
        result = self.run_publisher("status-error", "release")
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("Cannot inspect Git checkout status", result.stderr)
        calls = self.trace.read_text()
        self.assertNotIn("git rev-parse", calls)
        self.assertNotIn("gh ", calls)
