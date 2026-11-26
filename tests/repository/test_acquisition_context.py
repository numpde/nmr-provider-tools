"""Context export streams only selected regular files and verifies emitted bytes."""

import gc
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from acquisition_tool import context


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()

        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.source = self.root / "source"

        self.source.write_bytes(b"source bytes")

    # silent-defaults: allow-contract -- Test entries use one source fixture and destination unless a scenario overrides them.
    def entry(self, destination="app/source", source_path=None, content=b"source bytes", executable=False):
        return {
            "path": str(source_path or self.source),
            "destination": destination,
            "sha256": hashlib.sha256(content).hexdigest(),
            "executable": executable,
        }

    def test_tar_bytes_and_normalized_modes(self):
        self.source.chmod(0o777)
        output = io.BytesIO()

        context.stream_context([self.entry(), self.entry("bin/start", executable=True)], output)

        with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
            self.assertEqual(["app/source", "bin/start"], archive.getnames())
            self.assertEqual(b"source bytes", archive.extractfile("app/source").read())
            self.assertEqual([entry.mode for entry in archive], [0o644, 0o755])
            self.assertTrue(all((entry.uid == entry.gid == entry.mtime == 0) for entry in archive))

    def test_reject_destinations_before_output(self):
        for destination in [
            "",
            "/root",
            "a/../b",
            "./a",
            "a//b",
            "a/",
            "a\\b",
            (
                "a\n"
                "b"
            ),
        ]:
            with self.subTest(destination=destination):
                output = io.BytesIO()

                with self.assertRaises(ValueError):
                    context.stream_context([self.entry(destination)], output)

                self.assertEqual(b"", output.getvalue())

    def test_reject_collisions_in_both_orders(self):
        for destinations in [("a", "a"), ("a", "a/b"), ("a/b", "a")]:
            with self.subTest(destinations=destinations):
                with self.assertRaisesRegex(ValueError, "occurs more than once|parent path"):
                    context.stream_context([self.entry(name) for name in destinations], io.BytesIO())

    def test_reject_all_source_symlinks_and_nonregular_files(self):
        link = self.root / "link"

        link.symlink_to(self.source)
        parent_link = self.root / "parent-link"

        parent_link.symlink_to(self.root, target_is_directory=True)

        for source_path in [link, parent_link / "source", self.root]:
            with self.subTest(source_path=source_path):
                with self.assertRaises((OSError, ValueError)):
                    context.stream_context([self.entry(source_path=source_path)], io.BytesIO())

        fifo = self.root / "fifo"

        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValueError, "regular file"):
            context.stream_context([self.entry(source_path=fifo)], io.BytesIO())

    def test_late_mutation_hashes_actual_emitted_bytes(self):
        original = context._HashReader.read
        changed = False

        def mutate(reader, size):
            nonlocal changed

            if not changed:
                self.source.write_bytes(b"mutant bytes")
                changed = True

            return original(reader, size)

        with patch.object(context._HashReader, "read", mutate):
            with self.assertRaisesRegex(ValueError, "emitted SHA-256"):
                context.stream_context([self.entry()], io.BytesIO())

    def test_truncation_and_growth_fail(self):
        original = context._HashReader.read

        for (content, message) in [(b"short", "unexpected end"), (b"source bytes extra", "grew")]:
            self.source.write_bytes(b"source bytes")
            changed = False

            def mutate(reader, size):
                nonlocal changed

                if not changed:
                    self.source.write_bytes(content)
                    changed = True

                return original(reader, size)

            with self.subTest(content=content), patch.object(context._HashReader, "read", mutate):
                with self.assertRaisesRegex((OSError, ValueError), message):
                    context.stream_context([self.entry()], io.BytesIO())

    def test_large_stream_has_bounded_reads_and_no_buffered_output(self):
        content = b"x" * (4 * 1024 * 1024)

        self.source.write_bytes(content)
        original = context._HashReader.read
        sizes = []

        def read(reader, size):
            sizes.append(size)

            return original(reader, size)

        class Sink:
            total = 0

            def write(self, content):
                self.total += len(content)

                return len(content)

            def flush(self):
                pass

        sink = Sink()

        with patch.object(context._HashReader, "read", read):
            context.stream_context([self.entry(content=content)], sink)

        self.assertGreater(sink.total, len(content))
        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), 1024 * 1024)

    def test_cli_late_hash_failure_is_nonzero(self):
        manifest = self.root / "manifest.json"

        # Emit substantial file bytes before digest verification.
        self.source.write_bytes(b"changed" * 10000)

        manifest.write_text(json.dumps([self.entry()]))

        process_outcome = subprocess.run(
            [sys.executable, context.__file__, "--manifest", str(manifest)],
            capture_output=True,
        )

        self.assertEqual(1, process_outcome.returncode)
        self.assertIn(b"emitted SHA-256", process_outcome.stderr)
        self.assertGreater(len(process_outcome.stdout), 0)
        self.assertNotIn(b"Traceback", process_outcome.stderr)

    def test_short_output_is_rejected(self):
        class ShortSink:
            def __init__(self, count):
                self.count = count
                self.calls = 0

            def write(self, content):
                self.calls += 1

                return self.count

            def flush(self):
                raise AssertionError("Failed output must not be flushed.")

        for count in [0, 1, None]:
            with self.subTest(count=count):
                output = ShortSink(count)

                with self.assertRaisesRegex(OSError, "write accepted"):
                    context.stream_context([], output)

                gc.collect()
                self.assertEqual(1, output.calls)

    def test_failure_and_cancellation_do_not_write_during_cleanup(self):
        class RecordingSink(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.writes = 0
                self.flushes = 0

            def write(self, content):
                self.writes += 1

                return super().write(content)

            def flush(self):
                self.flushes += 1

                return super().flush()

        for failure in [None, KeyboardInterrupt("cancelled")]:
            with self.subTest(failure=failure):
                output = RecordingSink()
                unraisable = []
                entry = self.entry(content=b"different digest")

                with patch.object(sys, "unraisablehook", unraisable.append):
                    try:
                        if failure is None:
                            context.stream_context([entry], output)
                        else:
                            with patch.object(context._HashReader, "read", side_effect=failure):
                                context.stream_context([entry], output)
                    except (ValueError, KeyboardInterrupt) as error:
                        if failure is None:
                            self.assertIsInstance(error, ValueError)
                            self.assertIn("emitted SHA-256", str(error))
                        else:
                            self.assertIs(error, failure)

                        # Capture before the traceback releases the archive.
                        writes_at_failure = output.writes

                        bytes_at_failure = output.getvalue()
                        error.__traceback__ = None
                    else:
                        self.fail("Context export should fail before finalization.")

                    gc.collect()
                    self.assertEqual(output.writes, writes_at_failure)
                    self.assertEqual(output.getvalue(), bytes_at_failure)
                    self.assertEqual(0, output.flushes)
                    self.assertEqual(unraisable, [])

    def test_broken_output_keeps_the_write_error_without_retrying(self):
        class BrokenSink:
            calls = 0

            def write(self, content):
                self.calls += 1

                raise BrokenPipeError("builder stopped")

            def flush(self):
                raise RuntimeError("must not hide write failure")

        self.source.write_bytes(b"x" * 32768)
        output = BrokenSink()
        unraisable = []

        with patch.object(sys, "unraisablehook", unraisable.append):
            with self.assertRaisesRegex(BrokenPipeError, "builder stopped"):
                context.stream_context([self.entry()], output)

            gc.collect()
            self.assertEqual(1, output.calls)
            self.assertEqual(unraisable, [])

    def test_cli_broken_pipe_returns_failure_without_shutdown_retry(self):
        manifest = self.root / "manifest.json"

        manifest.write_text("[]")
        (read_descriptor, write_descriptor) = os.pipe()

        os.close(read_descriptor)

        try:
            process_outcome = subprocess.run(
                [sys.executable, context.__file__, "--manifest", str(manifest)],
                stdout=write_descriptor,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        finally:
            os.close(write_descriptor)

        self.assertEqual(1, process_outcome.returncode)
        self.assertIn(b"Broken pipe", process_outcome.stderr)
        self.assertNotIn(b"Exception ignored", process_outcome.stderr)
        self.assertNotIn(b"Traceback", process_outcome.stderr)


if __name__ == "__main__":
    unittest.main()
