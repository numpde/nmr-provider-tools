"""Test-only access to the complete pinned public provider HTTP release.

Copied source and content identity are recorded beside fixtures/nmr_api_v1.
This authenticates selected fixture bytes, not a publisher or a live server.
No release loader, schema engine or sibling import enters provider-core runtime.
"""

import hashlib
import json
import pathlib

RELEASE_ROOT = pathlib.Path(__file__).parents[1] / "fixtures/nmr_api_v1"
RELEASE_REF = "sha256:719055db7751605c0e5325ca23bcbe66cde631376d4b2b99523c8e4db75f0d01"
SOURCE_REVISION = "12a53e720fc87e1a4144c76203f8853d5afaf753"
MANIFEST_SHA256 = "dda8dc36db963f85ec9105567342dae4ba680476c7ab1114bd1e33acba65474b"


class PinnedApiRelease:
    def setUp(self):
        super().setUp()
        (self.openapi, self.schemas) = self.read_release(RELEASE_ROOT)

    def read_release(self, root):
        """Assert complete immutable identity before interpreting any artifact."""
        self.assertFalse(root.is_symlink(), root)
        release_paths = tuple(root.rglob("*"))

        for release_path in release_paths:
            self.assertFalse(release_path.is_symlink(), release_path)
            self.assertTrue(release_path.is_dir() or release_path.is_file(), release_path)

        raw = (root / "manifest.json").read_bytes()

        self.assertEqual(hashlib.sha256(raw).hexdigest(), MANIFEST_SHA256)
        manifest = json.loads(raw)

        self.assertEqual(manifest["release_ref"], RELEASE_REF)
        self.assertEqual(manifest["source_revision"], SOURCE_REVISION)

        material = (
            json
            .dumps(
                {"artifacts": manifest["artifacts"], "contract_id": manifest["contract_id"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            .encode()
        )

        self.assertEqual("sha256:" + hashlib.sha256(material).hexdigest(), RELEASE_REF)
        expected = {"manifest.json", *(entry["path"] for entry in manifest["artifacts"])}

        self.assertEqual({release_path.relative_to(root).as_posix() for release_path in release_paths if release_path.is_file()}, expected)
        documents = {}

        for entry in manifest["artifacts"]:
            raw = (root / entry["path"]).read_bytes()

            self.assertEqual(len(raw), entry["byte_length"], entry["path"])
            blob = b"blob " + str(len(raw)).encode() + b"\0" + raw

            self.assertEqual(hashlib.sha1(blob).hexdigest(), entry["git_blob_sha1"], entry["path"])
            documents[entry["path"]] = json.loads(raw)

        schemas = {schema_fields["$id"]: schema_fields for (artifact_path, schema_fields) in documents.items() if artifact_path.startswith("schemas/")}

        self.assertEqual(len(schemas), 16)

        return (documents["openapi/openapi.v1.json"], schemas)
