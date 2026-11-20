"""Stream a curated build context and locked pool artifacts as a tar archive."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

from packaging.pylock import PylockSelectError, PylockValidationError
from type_requirements import require_type

from wheelhouse_tool.__main__ import pool_lock, selected


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        content_chunk = self.stream.read(size)

        self.digest.update(content_chunk)

        return content_chunk


def open_source(path):
    """Open every source path component without following symbolic links."""
    parts = path.absolute().parts
    directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)

    try:
        for part in parts[1:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)

            os.close(directory)
            directory = next_directory

        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def stream_context(pool, source, selections, output, source_allowlist=None):
    """Use caller-selected source files and select pool files from their locks."""
    source_paths = {}
    source_hashes = {}
    source_modes = {}

    if source_allowlist is not None:
        source_hashes = {allowlist_entry["destination"]: allowlist_entry["sha256"] for allowlist_entry in source_allowlist}
        source_modes = {allowlist_entry["destination"]: allowlist_entry["executable"] for allowlist_entry in source_allowlist}

    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in source_hashes.values()):
        raise ValueError("Cannot stream build inputs: source files require SHA-256 hashes.")

    for executable_flag in source_modes.values():
        require_type(executable_flag, bool)

    def fail_walk(error):
        raise error

    if source_allowlist is None:
        if not source.is_dir():
            raise ValueError("Cannot stream build inputs: the curated source context is missing.")

        paths = (
            (Path(root) / name, (Path(root) / name).relative_to(source).as_posix())
            for (root, directories, files) in os.walk(source, followlinks=False, onerror=fail_walk)
            for name in sorted(directories + files)
        )
    else:
        paths = ((Path(allowlist_entry["path"]), allowlist_entry["destination"]) for allowlist_entry in source_allowlist)

    for (source_path, source_relative_path) in paths:
        destination = PurePosixPath(source_relative_path)

        if (
            destination.is_absolute() or
            (not destination.parts) or
            (str(destination) != source_relative_path) or
            any(((ord(character) < 32) or (ord(character) == 127)) for character in source_relative_path) or
            any((part in (".", "..")) for part in destination.parts)
        ):
            raise ValueError("Cannot stream build inputs: source destination must be a relative path.")

        if source_relative_path in source_paths:
            raise ValueError(f"Cannot stream {source_relative_path=}: source destination is duplicated.")

        metadata = source_path.lstat()

        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError(f"Cannot stream {source_relative_path=}: source must contain only directories and regular files.")

        if (source_allowlist is not None) and (not stat.S_ISREG(metadata.st_mode)):
            raise ValueError(f"Cannot stream {source_relative_path=}: allowlist entries must be regular files.")

        source_paths[source_relative_path] = source_path

    for source_relative_path in source_paths:
        if any(
            (
                ((parent.as_posix() in source_paths) and (not source_paths[parent.as_posix()].is_dir()))
                for parent in PurePosixPath(source_relative_path).parents
            ),
        ):
            raise ValueError(f"Cannot stream {source_relative_path=}: a source file occupies its parent path.")

    artifacts = {}

    for selection in selections:
        if "lock" in selection:
            if (not {"lock", "directory"}.issubset(selection)) or (set(selection) - {"lock", "directory", "prefer_sdist", "require_sdist", "source_lock"}):
                raise ValueError("Cannot stream build inputs: lock and explicit artifact entries cannot be mixed.")
        elif set(selection) != {"directory", "filename", "sha256"}:
            raise ValueError("Cannot stream build inputs: expected an explicit artifact entry.")

        directory = PurePosixPath(selection["directory"])

        if (
            directory.is_absolute() or
            (not directory.parts) or
            (str(directory) != selection["directory"]) or
            any(((ord(character) < 32) or (ord(character) == 127)) for character in selection["directory"]) or
            any((part in (".", "..")) for part in directory.parts)
        ):
            raise ValueError("Cannot stream build inputs: artifact directory must be a relative path.")

        if "lock" in selection:
            lock = Path(selection["lock"])
            source_lock = None

            if "source_lock" in selection:
                source_lock = Path(selection["source_lock"])

            preferred = ()
            required = ()

            if "prefer_sdist" in selection:
                preferred = selection["prefer_sdist"]

            if "require_sdist" in selection:
                required = selection["require_sdist"]

            inputs = [
                (artifact_filename, digest)
                for (artifact_filename, digest, *_) in selected(
                    lock,
                    preferred,
                    required,
                    source_lock=source_lock,
                )
            ]
        else:
            inputs = [(selection["filename"], selection["sha256"])]

        for (artifact_filename, digest) in inputs:
            if (
                (not artifact_filename) or
                (artifact_filename in (".", "..")) or
                ("/" in artifact_filename) or
                ("\\" in artifact_filename) or
                any(((ord(character) < 32) or (ord(character) == 127)) for character in artifact_filename) or
                (not re.fullmatch(r"[0-9a-f]{64}", digest))
            ):
                raise ValueError("Cannot stream build inputs: expected a filename and SHA-256 hash.")

            archive_destination = str(directory / artifact_filename)

            if (
                (archive_destination in source_paths) or
                any(source_relative_path.startswith(archive_destination + "/") for source_relative_path in source_paths) or
                any(
                    (
                        ((parent.as_posix() in source_paths) and (not source_paths[parent.as_posix()].is_dir()))
                        for parent in PurePosixPath(archive_destination).parents
                        if str(parent) != "."
                    ),
                )
            ):
                raise ValueError(f"Cannot stream {archive_destination=}: it collides with the curated source context.")

            if (archive_destination in artifacts) and (artifacts[archive_destination] != (artifact_filename, digest)):
                raise ValueError(f"Cannot stream {archive_destination=}: selections conflict.")

            artifacts[archive_destination] = (artifact_filename, digest)

    for archive_destination in artifacts:
        if any((parent.as_posix() in artifacts) for parent in PurePosixPath(archive_destination).parents):
            raise ValueError(f"Cannot stream {archive_destination=}: an artifact occupies its parent path.")

    with pool_lock(pool, exclusive=False) as directory_fd:
        # Callers must check this producer's status as well as the build status.
        tar = tarfile.open(fileobj=output, mode="w|")

        for (source_relative_path, source_path) in sorted(source_paths.items()):
            metadata = source_path.lstat()
            info = tarfile.TarInfo(source_relative_path)

            if (source_relative_path not in source_hashes) and (stat.S_ISDIR(metadata.st_mode)):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755

                tar.addfile(info)
            else:
                descriptor = open_source(source_path)

                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise ValueError(f"Cannot stream {source_relative_path=}: expected a regular source file.")

                with os.fdopen(descriptor, "rb") as source_stream:
                    metadata = os.fstat(source_stream.fileno())

                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError(f"Cannot stream {source_relative_path=}: expected a regular source file.")

                    if (
                        (source_relative_path in source_modes) and
                        ((source_is_executable := bool(metadata.st_mode & 0o111)) != (expected_executable := source_modes[source_relative_path]))
                    ):
                        raise ValueError(f"For source {source_relative_path=}, {source_is_executable=} should match {expected_executable=}")

                    if metadata.st_mode & 0o111:
                        info.mode = 0o755
                    else:
                        info.mode = 0o644

                    info.size = metadata.st_size
                    reader = HashReader(source_stream)

                    tar.addfile(info, reader)

                    if (
                        (source_relative_path in source_hashes) and
                        ((source_sha256 := reader.digest.hexdigest()) != (expected_sha256 := source_hashes[source_relative_path]))
                    ):
                        raise ValueError(f"For source {source_relative_path=}, {source_sha256=} should match {expected_sha256=}")

        for (archive_destination, (artifact_filename, expected_sha256)) in sorted(artifacts.items()):
            descriptor = os.open(artifact_filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)

            with os.fdopen(descriptor, "rb") as artifact_stream:
                metadata = os.fstat(artifact_stream.fileno())

                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"Cannot stream {artifact_filename=}: expected a regular file.")

                info = tarfile.TarInfo(archive_destination)
                info.size = metadata.st_size
                info.mode = 0o644
                reader = HashReader(artifact_stream)

                tar.addfile(info, reader)

                artifact_sha256 = reader.digest.hexdigest()

                if not (artifact_sha256 == expected_sha256):
                    raise ValueError(f"For artifact {artifact_filename=}, {artifact_sha256=} should match {expected_sha256=}")

        tar.close()
        output.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--pool", type=Path, required=True)
    sources = parser.add_mutually_exclusive_group(required=True)

    sources.add_argument("--source", type=Path)
    sources.add_argument("--source-allowlist", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        with args.manifest.open() as stream:
            selections = json.load(stream)

        if args.source_allowlist:
            with args.source_allowlist.open() as stream:
                allowlist = json.load(stream)
        else:
            allowlist = None

        stream_context(args.pool, args.source, selections, sys.stdout.buffer, allowlist)
    # silent-defaults: allow-exception-outcome -- Unsafe streaming inputs fail with a nonzero CLI status before a context is admitted.
    except (OSError, ValueError, KeyError, TypeError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
