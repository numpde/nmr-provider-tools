"""Stream caller-selected regular files as a verified tar build context.

The caller must check this producer's status as well as the builder's status.
Bytes already written to the output cannot be withdrawn after a late failure.
"""

import argparse
import hashlib
import json
import os
import pathlib
from pathlib import PurePosixPath
import re
import stat
import sys
import tarfile


def _context_files(manifest):
    # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
    if not isinstance(manifest, (expected_type := list)):
        raise TypeError(f"Cannot stream context: {type(manifest)=} ≠ {expected_type=}; provide a list of file entries.")

    context_files = {}

    for entry in manifest:
        # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
        if not isinstance(entry, (expected_type := dict)):
            raise TypeError(f"Cannot stream context: {type(entry)=} ≠ {expected_type=}; each file entry must be a mapping.")

        if not (set(entry) == {"path", "destination", "sha256", "executable"}):
            raise ValueError(f"Cannot stream context: {set(entry)=} must contain exactly path, destination, sha256 and executable.")

        destination = entry["destination"]

        # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
        if not isinstance(destination, (expected_type := str)):
            raise TypeError(f"Cannot stream context: {type(destination)=} ≠ {expected_type=}; each destination must be a string.")

        relative = PurePosixPath(destination)

        if not (
            relative.parts and
            not relative.is_absolute() and
            (str(relative) == destination) and
            (".." not in relative.parts) and
            ("\\" not in destination) and
            all(((ord(character) >= 32) and (ord(character) != 127)) for character in destination)
        ):
            raise ValueError(f"Cannot stream {destination=!r}: use a canonical relative path without dot segments or control characters.")

        if not (destination not in context_files):
            raise ValueError(f"Cannot stream {destination=!r}: it occurs more than once in the manifest.")

        source_path = entry["path"]

        # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
        if not isinstance(source_path, (expected_type := str)):
            raise TypeError(f"Cannot stream {destination=!r}: {type(source_path)=} ≠ {expected_type=}; path must be a nonempty string.")

        if not (source_path):
            raise TypeError(f"Cannot stream {destination=!r}: {source_path=!r} must be a nonempty string.")

        digest = entry["sha256"]

        # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
        if not isinstance(digest, (expected_type := str)):
            raise TypeError(f"Cannot stream {destination=!r}: {type(digest)=} ≠ {expected_type=}; sha256 must be a string.")

        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Cannot stream {destination=!r}: {digest=!r} must contain 64 lowercase hexadecimal characters.")

        executable = entry["executable"]

        # repo-check: retain-type-guard -- The standard-library-only context entrypoint cannot import require_instance.
        if not isinstance(executable, (expected_type := bool)):
            raise TypeError(f"Cannot stream {destination=!r}: {type(executable)=} ≠ {expected_type=}; executable must be a boolean.")

        context_files[destination] = (pathlib.Path(source_path), digest, executable)

    for destination in context_files:
        for parent in PurePosixPath(destination).parents:
            if not (parent.as_posix() not in context_files):
                raise ValueError(f"Cannot stream {destination=!r}: file {parent.as_posix()=!r} occupies its parent path.")

    return context_files


def _open_source(source_path):
    """Walk from the root using directory descriptors so no component follows links."""
    parts = source_path.absolute().parts
    directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    try:
        for part in parts[1:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)

            os.close(directory)
            directory = next_directory

        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


class _HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        chunk = self.stream.read(size)

        self.digest.update(chunk)

        return chunk


class _ContextOutput:
    def __init__(self, output):
        self.output = output
        self.position = 0

    def tell(self):
        return self.position

    def write(self, content):
        written = self.output.write(content)

        if not (written == len(content)):
            raise OSError(f"Cannot complete context output: write accepted {written=!r} of {len(content)=} bytes.")

        self.position += written

        return written


def stream_context(manifest, output):
    """Emit one tar stream; success requires hashes of all emitted file bytes to match.

    File contents are read in bounded chunks. The captured manifest owns archive
    destinations and executable modes; source permissions do not set tar modes.
    The output remains open and may contain partial bytes when this function raises.
    """
    context_files = _context_files(manifest)

    # Direct writes avoid tar's stream buffer and its finalizer flushing after
    # failure. The context manager writes the archive trailer only on success.
    with tarfile.open(fileobj=_ContextOutput(output), mode="w", format=tarfile.PAX_FORMAT) as archive:
        for (destination, (source_path, expected_sha256, executable)) in sorted(context_files.items()):
            descriptor = _open_source(source_path)

            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())

                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"Cannot stream {destination=!r} from {str(source_path)=!r}: the source must be a regular file.")

                info = tarfile.TarInfo(destination)
                info.size = metadata.st_size

                # silent-defaults: allow-contract -- The manifest executable flag selects the normalized archive mode.
                info.mode = 0o755 if executable else 0o644

                reader = _HashReader(source)

                archive.addfile(info, reader)
                actual_sha256 = reader.digest.hexdigest()

                if not (actual_sha256 == expected_sha256):
                    raise ValueError(
                        f"Cannot complete context {destination=!r}: emitted SHA-256 {actual_sha256=!s} "
                        f"differs from manifest SHA-256 {expected_sha256=!s}; output may already contain file bytes."
                    )

                if not (source.read(1) == b""):
                    raise ValueError(f"Cannot complete context {destination=!r}: the source grew during streaming; output may already contain file bytes.")

    output.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)

    try:
        with args.manifest.open(encoding="utf-8") as stream:
            manifest = json.load(stream)

        # Own an unbuffered descriptor so a broken pipe cannot be retried by
        # Python's stdout buffer during interpreter shutdown.
        with os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0) as output:
            stream_context(manifest, output)
    # silent-defaults: allow-exception-outcome -- Export failures return nonzero after reporting the cause; partial output is never success.
    except (OSError, ValueError, TypeError, tarfile.TarError) as error:
        print(f"Context export failed: {error=!s}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
