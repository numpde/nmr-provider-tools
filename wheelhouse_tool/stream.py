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

from wheelhouse_tool.__main__ import pool_lock, selected


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


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
    source_hashes = {} if source_allowlist is None else {item["destination"]: item["sha256"] for item in source_allowlist}
    source_modes = {} if source_allowlist is None else {item["destination"]: item["executable"] for item in source_allowlist}
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in source_hashes.values()):
        raise ValueError("Cannot stream build inputs: source files require SHA-256 hashes.")
    if any(type(value) is not bool for value in source_modes.values()):
        raise ValueError("Cannot stream build inputs: source executable flags must be boolean.")
    def fail_walk(error):
        raise error

    if source_allowlist is None:
        if not source.is_dir():
            raise ValueError("Cannot stream build inputs: the curated source context is missing.")
        paths = ((Path(root) / name, (Path(root) / name).relative_to(source).as_posix())
                 for root, directories, files in os.walk(source, followlinks=False, onerror=fail_walk)
                 for name in sorted(directories + files))
    else:
        paths = ((Path(item["path"]), item["destination"]) for item in source_allowlist)
    for path, name in paths:
        destination = PurePosixPath(name)
        if (destination.is_absolute() or not destination.parts or str(destination) != name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or any(part in (".", "..") for part in destination.parts)):
            raise ValueError("Cannot stream build inputs: source destination must be a relative path.")
        if name in source_paths:
            raise ValueError(f"Cannot stream {name}: source destination is duplicated.")
        metadata = path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError(f"Cannot stream {name}: source must contain only directories and regular files.")
        if source_allowlist is not None and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Cannot stream {name}: allowlist entries must be regular files.")
        source_paths[name] = path
    for name in source_paths:
        if any(parent.as_posix() in source_paths and not source_paths[parent.as_posix()].is_dir()
               for parent in PurePosixPath(name).parents):
            raise ValueError(f"Cannot stream {name}: a source file occupies its parent path.")
    artifacts = {}
    for selection in selections:
        if "lock" in selection:
            if not {"lock", "directory"}.issubset(selection) or set(selection) - {"lock", "directory", "prefer_sdist", "require_sdist"}:
                raise ValueError("Cannot stream build inputs: lock and explicit artifact entries cannot be mixed.")
        elif set(selection) != {"directory", "filename", "sha256"}:
            raise ValueError("Cannot stream build inputs: expected an explicit artifact entry.")
        directory = PurePosixPath(selection["directory"])
        if (directory.is_absolute() or not directory.parts or str(directory) != selection["directory"]
                or any(ord(character) < 32 or ord(character) == 127 for character in selection["directory"])
                or any(part in (".", "..") for part in directory.parts)):
            raise ValueError("Cannot stream build inputs: artifact directory must be a relative path.")
        if "lock" in selection:
            lock = Path(selection["lock"])
            inputs = [(name, digest) for name, digest, *_ in selected(
                lock, selection.get("prefer_sdist", ()), selection.get("require_sdist", ()))]
        else:
            inputs = [(selection["filename"], selection["sha256"])]
        for name, digest in inputs:
            if (not name or name in (".", "..") or "/" in name or "\\" in name
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("Cannot stream build inputs: expected a filename and SHA-256 hash.")
            destination = str(directory / name)
            if (destination in source_paths or any(name.startswith(destination + "/") for name in source_paths)
                    or any(parent.as_posix() in source_paths and not source_paths[parent.as_posix()].is_dir()
                           for parent in PurePosixPath(destination).parents if str(parent) != ".")):
                raise ValueError(f"Cannot stream {destination}: it collides with the curated source context.")
            if destination in artifacts and artifacts[destination] != (name, digest):
                raise ValueError(f"Cannot stream {destination}: selections conflict.")
            artifacts[destination] = (name, digest)
    for destination in artifacts:
        if any(parent.as_posix() in artifacts for parent in PurePosixPath(destination).parents):
            raise ValueError(f"Cannot stream {destination}: an artifact occupies its parent path.")
    with pool_lock(pool, exclusive=False) as directory_fd:
        # Callers must check this producer's status as well as the build status.
        tar = tarfile.open(fileobj=output, mode="w|")
        for name, path in sorted(source_paths.items()):
            metadata = path.lstat()
            info = tarfile.TarInfo(name)
            if name not in source_hashes and stat.S_ISDIR(metadata.st_mode):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                descriptor = open_source(path)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise ValueError(f"Cannot stream {name}: expected a regular source file.")
                with os.fdopen(descriptor, "rb") as file:
                    metadata = os.fstat(file.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError(f"Cannot stream {name}: expected a regular source file.")
                    if name in source_modes and bool(metadata.st_mode & 0o111) != source_modes[name]:
                        raise ValueError(f"Cannot stream {name}: source executable mode differs from the captured input.")
                    info.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                    info.size = metadata.st_size
                    reader = HashReader(file)
                    tar.addfile(info, reader)
                    if name in source_hashes and reader.digest.hexdigest() != source_hashes[name]:
                        raise ValueError(f"Cannot stream {name}: source bytes differ from the captured input.")
        for destination, (name, expected) in sorted(artifacts.items()):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(descriptor, "rb") as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"Cannot stream {name}: expected a regular file.")
                info = tarfile.TarInfo(destination)
                info.size = metadata.st_size
                info.mode = 0o644
                reader = HashReader(file)
                tar.addfile(info, reader)
                if reader.digest.hexdigest() != expected:
                    raise ValueError(f"Cannot stream {name}: bytes differ from the lock.")
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
    except (OSError, ValueError, KeyError, TypeError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
