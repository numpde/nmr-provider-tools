"""The shared pool preserves locked artifacts through acquisition failures."""

import hashlib
import fcntl
import io
import json
import os
import tarfile
from contextlib import contextmanager
import tempfile
import threading
import tomllib
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wheelhouse_tool import __main__ as tool
from wheelhouse_tool.stream import stream_context
from wheelhouse_tool.report import manifest as report_manifest
from wheelhouse_tool.identity import selection_identity
from wheelhouse_tool.locate import locate


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.content = b"locked artifact bytes"
        self.name = "example-1.0-py3-none-any.whl"
        (self.root / self.name).write_bytes(self.content)
        self.requests = []
        requests = self.requests

        class Server(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                requests.append(self.path)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Server, directory=str(self.root)))
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.lock = self.root / "pylock.toml"
        self.write_lock(hashlib.sha256(self.content).hexdigest())

    def write_lock(self, digest):
        self.lock.write_text(
            'lock-version = "1.0"\ncreated-by = "test"\n'
            '[[packages]]\nname = "example"\nversion = "1.0"\n'
            '[[packages.wheels]]\n'
            f'url = "http://127.0.0.1:{self.server.server_port}/{self.name}"\n'
            f'hashes = {{ sha256 = "{digest}" }}\n'
        )

    def test_missing_is_downloaded_once_and_cached_without_replacement(self):
        tool.run("acquire", self.lock, self.pool)
        path = self.pool / self.name
        inode = path.stat().st_ino
        tool.run("acquire", self.lock, self.pool)
        self.assertEqual(self.content, path.read_bytes())
        self.assertEqual(inode, path.stat().st_ino)
        self.assertEqual([f"/{self.name}"], self.requests)

    def test_conflict_is_preserved_without_network(self):
        path = self.pool / self.name
        path.write_bytes(b"different")
        inode = path.stat().st_ino
        with self.assertRaisesRegex(ValueError, "existing file is preserved"):
            tool.run("acquire", self.lock, self.pool)
        self.assertEqual((b"different", inode), (path.read_bytes(), path.stat().st_ino))
        self.assertEqual([], self.requests)

    def test_invalid_download_leaves_no_final_or_temporary_file(self):
        self.write_lock("0" * 64)
        with self.assertRaisesRegex(ValueError, "downloaded bytes differ"):
            tool.run("acquire", self.lock, self.pool)
        self.assertEqual([".wheelhouse-acquire.lock"], [path.name for path in self.pool.iterdir()])

    def test_warm_reader_proceeds_while_acquisitions_share_one_download(self):
        cached = "cached-1.0-py3-none-any.whl"
        (self.pool / cached).write_bytes(self.content)
        digest = hashlib.sha256(self.content).hexdigest()
        package = SimpleNamespace(name="cached")
        cached_artifacts = [(cached, digest, "", package, None)]
        started, resume = threading.Event(), threading.Event()
        original_download = tool.download
        errors = []

        def slow_download(*args):
            started.set()
            if not resume.wait(5):
                raise TimeoutError("test did not resume download")
            original_download(*args)

        def acquire():
            try:
                tool.run("acquire", self.lock, self.pool)
            except Exception as error:
                errors.append(error)

        def read_cached():
            try:
                tool.run("check", None, self.pool, artifacts=cached_artifacts)
            except Exception as error:
                errors.append(error)

        with patch.object(tool, "download", slow_download):
            first = threading.Thread(target=acquire)
            second = threading.Thread(target=acquire)
            first.start()
            try:
                self.assertTrue(started.wait(5))
                second.start()
                reader = threading.Thread(target=read_cached)
                reader.start()
                reader.join(2)
                self.assertFalse(reader.is_alive(), "warm reader waited for the download")
            finally:
                resume.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([f"/{self.name}"], self.requests)

    def test_legacy_exclusive_pool_writer_excludes_acquisition(self):
        started = threading.Event()
        original_download = tool.download
        errors = []

        def capture_download(*args):
            started.set()
            original_download(*args)

        def acquire():
            try:
                tool.run("acquire", self.lock, self.pool)
            except Exception as error:
                errors.append(error)

        directory = os.open(self.pool, os.O_RDONLY | os.O_DIRECTORY)
        with patch.object(tool, "download", capture_download):
            fcntl.flock(directory, fcntl.LOCK_EX)
            worker = threading.Thread(target=acquire)
            worker.start()
            try:
                self.assertFalse(started.wait(0.25))
            finally:
                os.close(directory)
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(started.is_set())
        self.assertEqual([], errors)

    def test_check_missing_does_not_download(self):
        with self.assertRaisesRegex(FileNotFoundError, "missing"):
            tool.run("check", self.lock, self.pool)
        self.assertEqual([], self.requests)

    def test_stream_includes_only_selected_pool_files_and_curated_source(self):
        (self.pool / self.name).write_bytes(self.content)
        (self.pool / "unselected.whl").write_bytes(b"not selected")
        source = self.root / "context"
        source.mkdir()
        (source / "Dockerfile").write_text("FROM scratch\n")
        result = io.BytesIO()
        stream_context(self.pool, source, [{"lock": str(self.lock), "directory": "wheels/main"}], result)
        result.seek(0)
        with tarfile.open(fileobj=result) as archive:
            self.assertEqual({"Dockerfile", "wheels/main/" + self.name}, set(archive.getnames()))
            self.assertEqual(self.content, archive.extractfile("wheels/main/" + self.name).read())

    def test_stream_refuses_changed_artifact_and_source_link(self):
        (self.pool / self.name).write_bytes(b"wrong")
        source = self.root / "context"
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            stream_context(self.pool, source, [{"lock": str(self.lock), "directory": "wheels"}], io.BytesIO())
        (source / "link").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "only directories and regular files"):
            stream_context(self.pool, source, [], io.BytesIO())

    def test_stream_accepts_explicit_source_files_without_staging(self):
        output = io.BytesIO()
        stream_context(self.pool, None, [], output, [{"path": str(self.lock), "destination": "inputs/pylock.toml",
                                                     "sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                                                     "executable": False}])
        output.seek(0)
        with tarfile.open(fileobj=output) as archive:
            self.assertEqual(["inputs/pylock.toml"], archive.getnames())
            self.assertEqual(self.lock.read_bytes(), archive.extractfile("inputs/pylock.toml").read())

    def test_stream_refuses_untrusted_source_paths_and_mismatched_identity(self):
        entry = {"path": str(self.lock), "destination": "a/./file", "sha256": "0" * 64, "executable": False}
        with self.assertRaisesRegex(ValueError, "relative path"):
            stream_context(self.pool, None, [], io.BytesIO(), [entry])
        entry["destination"] = "a/file"
        with self.assertRaisesRegex(ValueError, "source bytes differ"):
            stream_context(self.pool, None, [], io.BytesIO(), [entry])
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        entry["path"] = str(alias / self.lock.name)
        entry["sha256"] = hashlib.sha256(self.lock.read_bytes()).hexdigest()
        with self.assertRaises(OSError):
            stream_context(self.pool, None, [], io.BytesIO(), [entry])

    def test_allowlisted_file_cannot_be_replaced_with_directory(self):
        entry = {"path": str(self.lock), "destination": "input", "sha256": "0" * 64, "executable": False}
        original_lock = tool.pool_lock

        @contextmanager
        def replace_source(pool, exclusive):
            self.lock.unlink()
            self.lock.mkdir()
            with original_lock(pool, exclusive) as descriptor:
                yield descriptor

        with patch("wheelhouse_tool.stream.pool_lock", replace_source):
            with self.assertRaisesRegex(ValueError, "regular source file"):
                stream_context(self.pool, None, [], io.BytesIO(), [entry])

    def test_report_projects_only_authorized_pool_artifacts(self):
        def report(url):
            return {"version": "1", "install": [{"download_info": {"url": url,
                    "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}

        result = report_manifest(report("file:///pool/torch-1.0%2Bcpu.whl"), "/pool", "wheels", set())
        self.assertEqual("torch-1.0+cpu.whl", result[0]["filename"])
        for url in ("https://host/pool/a.whl", "file:///pool/a%2Fb.whl", "file:///pool/a%FF.whl", "file:///pool/a\n.whl",
                    "file:///pool/a%zz.whl", "file:///pool/a%0A.whl", "file:///pool/a.whl?query"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                report_manifest(report(url), "/pool", "wheels", set())
        source = "antlr-1.0.tar.gz"
        with self.assertRaises(ValueError):
            report_manifest(report("file:///pool/" + source), "/pool", "wheels", set())
        self.assertEqual(source, report_manifest(report("file:///pool/" + source), "/pool", "wheels", {source})[0]["filename"])
        for invalid in (None, [], {"version": "1", "install": []}):
            with self.subTest(report=invalid), self.assertRaises(ValueError):
                report_manifest(invalid, "/pool", "wheels", set())

    def test_selection_identity_is_stable_and_changes_with_selected_bytes(self):
        first = {"directory": "wheels/main", "filename": "first.whl", "sha256": "a" * 64}
        second = {"directory": "wheels/native", "filename": "second.whl", "sha256": "b" * 64}
        self.assertEqual(selection_identity([first, second]), selection_identity([second, first, first]))
        self.assertNotEqual(selection_identity([first]), selection_identity([first | {"sha256": "c" * 64}]))
        with self.assertRaisesRegex(ValueError, "explicitly identify"):
            selection_identity([first | {"lock": "other.toml"}])
        with self.assertRaisesRegex(ValueError, "cannot be mixed"):
            stream_context(self.pool, self.root, [first | {"lock": str(self.lock)}], io.BytesIO())
        for entry in (first | {"directory": "wheels/one\x00two"}, first | {"filename": "bad\x00.whl"}):
            with self.assertRaises(ValueError):
                selection_identity([entry])
            with self.assertRaises(ValueError):
                stream_context(self.pool, None, [entry], io.BytesIO(), [])

    def test_locator_uses_index_metadata_and_existing_hashes_without_downloading_wheels(self):
        digest = "a" * 64
        direct = "b" * 64
        self.lock.write_text(
            '--index-url https://example.invalid/simple\n'
            'example==1.0 \\\n    --hash=sha256:' + digest + '\n'
            'torch @ https://example.invalid/torch-2.3.0%2Bcpu-cp312-cp312-linux_x86_64.whl#sha256=' + direct + ' \\\n'
            '    --hash=sha256:' + direct + '\n'
        )
        metadata = {"name": "example", "files": [
            {"filename": "example-1.0-py3-none-any.whl", "url": "../../files/example-1.0-py3-none-any.whl", "hashes": {"sha256": digest}},
            {"filename": "example-1.0-py3-none-any.whl", "url": "../../files/other.whl", "hashes": {"sha256": "c" * 64}},
        ]}
        with patch("wheelhouse_tool.locate.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(metadata).encode())
            projected_text = locate(self.lock)
            projected = tomllib.loads(projected_text)
        self.assertEqual(1, opener.return_value.open.call_count)
        self.assertEqual("https://example.invalid/files/example-1.0-py3-none-any.whl", projected["packages"][0]["wheels"][0]["url"])
        self.assertEqual(direct, projected["packages"][1]["archive"]["hashes"]["sha256"])
        derived = self.root / "pylock.derived.toml"
        derived.write_text(projected_text)
        with self.assertRaisesRegex(ValueError, "matching source requirements lock"):
            tool.selected(derived, [])
        self.assertEqual(2, len(tool.selected(derived, [], source_lock=self.lock)))
        derived.write_text(projected_text.replace(digest, "c" * 64))
        with self.assertRaisesRegex(ValueError, "differs from its source lock"):
            tool.selected(derived, [], source_lock=self.lock)
        derived.write_text(projected_text.replace("https://example.invalid/torch-", "https://other.invalid/torch-"))
        with self.assertRaisesRegex(ValueError, "different locked URL"):
            tool.selected(derived, [], source_lock=self.lock)
        derived.write_text(projected_text.split("[tool.nmr-provider-tools]")[0])
        with self.assertRaisesRegex(ValueError, "source lock binding is missing"):
            tool.selected(derived, [], source_lock=self.lock)

    def test_locator_freezes_a_compatible_cached_wheel(self):
        cached = "example-1.0-py3-none-any.whl"
        native = "example-1.0-cp312-cp312-linux_x86_64.whl"
        cached_hash = hashlib.sha256(self.content).hexdigest()
        native_hash = hashlib.sha256(b"native bytes").hexdigest()
        (self.pool / cached).write_bytes(self.content)
        (self.pool / native).write_bytes(b"conflicting bytes")
        self.lock.write_text('--index-url https://example.invalid/simple\nexample==1.0 \\\n'
                             f'    --hash=sha256:{cached_hash} \\\n    --hash=sha256:{native_hash}\n')
        metadata = {"name": "example", "files": [
            {"filename": name, "url": f"https://example.invalid/{name}", "hashes": {"sha256": digest}}
            for name, digest in ((cached, cached_hash), (native, native_hash))
        ]}
        with patch("wheelhouse_tool.locate.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(metadata).encode())
            projected = locate(self.lock, allowed_sources=["example"], pool=self.pool)
        self.assertEqual(b"conflicting bytes", (self.pool / native).read_bytes())
        derived = self.root / "pylock.cached.toml"
        derived.write_text(projected)
        # An alternative appearing later cannot change this projection.
        (self.pool / native).write_bytes(b"native bytes")
        artifacts = tool.selected(derived, [], source_lock=self.lock)
        self.assertEqual([cached], [entry[0] for entry in artifacts])
        self.assertEqual(cached, tool.run("check", derived, self.pool, artifacts=artifacts)["artifacts"][0]["filename"])

    def test_locator_accepts_existing_direct_source_only_when_it_matches_the_pin(self):
        digest = "a" * 64
        self.lock.write_text('--index-url https://example.invalid/simple\ntorch==2.3.0+cpu \\\n    --hash=sha256:' + digest + '\n')
        url = "https://example.invalid/torch-2.3.0%2Bcpu-cp312-cp312-linux_x86_64.whl#sha256=" + digest
        projected = tomllib.loads(locate(self.lock, direct_sources=[url]))
        self.assertEqual(digest, projected["packages"][0]["archive"]["hashes"]["sha256"])
        with self.assertRaisesRegex(ValueError, "differs from its locked identity"):
            locate(self.lock, direct_sources=[url.replace(digest, "b" * 64)])
        with self.assertRaises(ValueError) as error:
            locate(self.lock, direct_sources=[url.replace("#sha256=", "?signature=private#sha256=")])
        self.assertNotIn("private", str(error.exception))

    def test_locator_handles_direct_only_and_required_source(self):
        digest = "a" * 64
        url = "https://example.invalid/torch-2.3.0%2Bcpu-cp312-cp312-linux_x86_64.whl#sha256=" + digest
        self.lock.write_text("torch @ " + url + " \\\n    --hash=sha256:" + digest + "\n")
        self.assertEqual("torch", tomllib.loads(locate(self.lock))["packages"][0]["name"])
        with self.assertRaisesRegex(ValueError, "differs from its locked identity"):
            locate(self.lock, required_sources=["torch"])

        self.lock.write_text('--index-url https://example.invalid/simple\nexample==1.0 \\\n    --hash=sha256:' + digest + '\n')
        metadata = {"name": "example", "files": [{"filename": "example-1.0-py3-none-any.whl",
                    "url": "https://example.invalid/example-1.0-py3-none-any.whl", "hashes": {"sha256": digest}}]}
        with patch("wheelhouse_tool.locate.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(metadata).encode())
            with self.assertRaisesRegex(ValueError, "required source distribution"):
                locate(self.lock, required_sources=["example"])

    def test_simultaneous_acquisition_reuses_the_first_result(self):
        errors = []

        def acquire():
            try:
                tool.run("acquire", self.lock, self.pool)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual([f"/{self.name}"], self.requests)

    def test_required_source_does_not_fall_back_to_wheel(self):
        with self.assertRaisesRegex(ValueError, "source distribution"):
            tool.run("check", self.lock, self.pool, require_sdist=["example"])

    def test_local_lock_preserves_the_selected_hash_and_uses_a_local_path(self):
        projected = tomllib.loads(tool.local_lock(self.lock, Path("/wheels")))
        artifact = projected["packages"][0]["wheels"][0]
        self.assertEqual("/wheels/" + self.name, artifact["path"])
        self.assertNotIn("url", artifact)
        self.assertEqual(hashlib.sha256(self.content).hexdigest(), artifact["hashes"]["sha256"])

    def test_source_must_be_explicitly_allowed(self):
        self.lock.write_text(
            'lock-version = "1.0"\ncreated-by = "test"\n'
            '[[packages]]\nname = "example"\nversion = "1.0"\n'
            '[packages.sdist]\nurl = "https://example.invalid/example-1.0.tar.gz"\n'
            f'hashes = {{ sha256 = "{"0" * 64}" }}\n'
        )
        with self.assertRaisesRegex(ValueError, "source distributions require"):
            tool.run("acquire", self.lock, self.pool)

    def test_direct_archives_obey_source_policy(self):
        def archive(name):
            self.lock.write_text(
                'lock-version = "1.0"\ncreated-by = "test"\n'
                '[[packages]]\nname = "example"\nversion = "1.0"\n'
                '[packages.archive]\n'
                f'url = "https://example.invalid/{name}"\n'
                f'hashes = {{ sha256 = "{"0" * 64}" }}\n'
            )

        archive("example-1.0.tar.gz")
        with self.assertRaisesRegex(ValueError, "source distributions require"):
            tool.selected(self.lock, [])
        self.assertEqual("example-1.0.tar.gz", tool.selected(self.lock, [], ["example"])[0][0])
        archive("example-1.0-py3-none-any.whl")
        self.assertEqual("example-1.0-py3-none-any.whl", tool.selected(self.lock, [])[0][0])
        with self.assertRaisesRegex(ValueError, "source distribution"):
            tool.selected(self.lock, [], ["example"])
        archive("example-1.0-cp27-cp27m-win32.whl")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            tool.selected(self.lock, [])
        archive("other-1.0.tar.gz")
        with self.assertRaisesRegex(ValueError, "does not match"):
            tool.selected(self.lock, [], ["example"])

    def test_credential_in_url_is_rejected_without_disclosure(self):
        lock = self.lock.read_text().replace("http://", "http://user:secret@")
        self.lock.write_text(lock)
        with self.assertRaises(ValueError) as error:
            tool.run("acquire", self.lock, self.pool)
        self.assertNotIn("secret", str(error.exception))

    def test_vanished_concurrent_winner_is_not_reported_as_success(self):
        with patch.object(tool.os, "link", side_effect=FileExistsError):
            with self.assertRaisesRegex(FileNotFoundError, "disappeared"):
                tool.run("acquire", self.lock, self.pool)
        self.assertEqual([".wheelhouse-acquire.lock"], [path.name for path in self.pool.iterdir()])
