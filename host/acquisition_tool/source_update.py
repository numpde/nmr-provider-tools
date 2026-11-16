"""Verify and explicitly replace a consumer's checked-in acquisition source.

Only standard-library code runs before package acquisition. The old, reviewed
updater checks the old snapshot and authenticates incoming archive bytes before
reading candidate metadata. Builds never call the network update implicitly.
Source maintenance requires quiescent readers; a directory exchange cannot
retire processes that already hold source files open.
"""

import argparse
from collections import Counter
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import TypedDict


class PayloadFileMetadata(TypedDict):
    """Size, mode and digest recorded for one acquisition payload file."""

    sha256: str
    size: int
    mode: int


class AcquisitionManifest(TypedDict):
    """The manifest shape validated by read_manifest before its callers use it."""

    release_version: str
    source_revision: str
    payload_files: dict[str, PayloadFileMetadata]
    dependency_inputs: dict[str, str]


def _json(data, description):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as error:
        error.add_note(f"Reading {description}.")
        raise


def _version(value):
    return type(value) is str and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)


def _checksum(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)


def read_pin(path):
    """Read the consumer's expected release; downloads never rewrite this pin."""
    (data, _metadata) = _regular_bytes(Path(path))
    pin = _json(data, f"release pin {path}")

    if not (type(pin) is (expected_type := dict)):
        raise TypeError(f"{path=}; {type(pin)=} ≠ {expected_type=}")

    if (
        (set(pin) != {"version", "archive_sha256"}) or
        (not _version(pin["version"])) or
        (not _checksum(pin["archive_sha256"]))
    ):
        raise ValueError(f"Cannot read release pin {path}: require version and archive_sha256.")

    return pin


def _relative_name(acquisition_path):
    if not (type(acquisition_path) is (expected_type := str)):
        raise TypeError(f"{type(acquisition_path)=} ≠ {expected_type=}")

    if (not acquisition_path) or ("\0" in acquisition_path):
        raise ValueError(f"Acquisition path must be nonempty and contain no nulls: {acquisition_path=}")

    path = PurePosixPath(acquisition_path)

    is_canonical_relative_path = (
        bool(path.parts) and
        (not path.is_absolute()) and
        (".." not in path.parts) and
        (path.as_posix() == acquisition_path)
    )

    if not (is_canonical_relative_path):
        raise ValueError(f"Acquisition inventory path is not a canonical relative file: {acquisition_path=}")

    return path


def read_manifest(data: str | bytes | bytearray) -> AcquisitionManifest:
    """Admit the recorded inventory, excluding only the manifest's own bytes."""
    manifest = _json(data, "acquisition-manifest.json")

    if not (type(manifest) is (expected_type := dict)):
        raise TypeError(f"{type(manifest)=} ≠ {expected_type=}")

    if set(manifest) != {
        "release_version",
        "source_revision",
        "payload_files",
        "dependency_inputs",
    }:
        raise ValueError("Acquisition manifest must identify its release, source, payload and dependency inputs.")

    if not _version(manifest["release_version"]):
        raise ValueError("Acquisition manifest has no exact release version or Git source revision.")

    if not (type((manifest_source_revision := manifest["source_revision"])) is (expected_type := str)):
        raise TypeError(f"{type(manifest_source_revision)=} ≠ {expected_type=}")

    if not re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"]):
        raise ValueError("Acquisition manifest has no exact release version or Git source revision.")

    entries = manifest["payload_files"]

    if not (type(entries) is (expected_type := dict)):
        raise TypeError(f"{type(entries)=} ≠ {expected_type=}")

    if not (entries) or "acquisition-manifest.json" in entries:
        raise ValueError("Acquisition manifest must inventory its payload without hashing itself.")

    for (name, entry) in entries.items():
        path = _relative_name(name)

        if any(parent.as_posix() in entries for parent in path.parents):
            raise ValueError(f"Acquisition payload file conflicts with a parent path: {name!r}.")

        if not (type(entry) is (expected_type := dict)):
            raise TypeError(f"{name=}; {type(entry)=} ≠ {expected_type=}")

        if not (set(entry) == {"sha256", "size", "mode"} and _checksum(entry["sha256"])):
            raise ValueError(f"Acquisition payload has invalid file metadata: {name!r}.")

        if not (type((entry_size := entry["size"])) is (expected_type := int)):
            raise TypeError(f"{name=}; {type(entry_size)=} ≠ {expected_type=}")

        if not (0 <= entry["size"]):
            raise ValueError(f"Acquisition payload has invalid file metadata: {name!r}.")

        if not (type((entry_mode := entry["mode"])) is (expected_type := int)):
            raise TypeError(f"{name=}; {type(entry_mode)=} ≠ {expected_type=}")

        if not (entry["mode"] in (0o644, 0o755)):
            raise ValueError(f"Acquisition payload has invalid file metadata: {name!r}.")

    dependencies = manifest["dependency_inputs"]

    if not (type(dependencies) is (expected_type := dict)):
        raise TypeError(f"{type(dependencies)=} ≠ {expected_type=}")

    for (name, checksum) in dependencies.items():
        if not (type(name) is (expected_name_type := str)):
            raise TypeError(f"{type(name)=} ≠ {expected_name_type=}")

        if not (type(checksum) is (expected_checksum_type := str)):
            raise TypeError(f"{type(checksum)=} ≠ {expected_checksum_type=}")

        if not name or not _checksum(checksum):
            raise ValueError("Acquisition dependency inputs must map artifact names to SHA-256 checksums.")

    return manifest


def _regular_bytes(path):
    """Read one opened regular file, without following links or blocking on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    try:
        metadata = os.fstat(descriptor)

        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Cannot read acquisition input {path}: require a regular file.")

        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return (stream.read(), metadata)
    finally:
        os.close(descriptor)


def _check_bytes(acquisition_rel_path, content, entry):
    actual_size = len(content)
    release_size = entry["size"]

    if actual_size != release_size:
        raise ValueError(f"Acquisition {acquisition_rel_path=}: {actual_size=} ≠ {release_size=}")

    actual_sha256 = hashlib.sha256(content).hexdigest()
    release_sha256 = entry["sha256"]

    if actual_sha256 != release_sha256:
        raise ValueError(f"Acquisition {acquisition_rel_path=}: {actual_sha256=} ≠ {release_sha256=}")


def _raise_walk_error(error: OSError) -> None:
    """Preserve a directory scan failure instead of reporting files as missing."""
    raise error


def verify(source_dir):
    """Check installed files against their OLD manifest and return that manifest.

    This deliberately takes no incoming pin: a maintainer can select the next
    release before updating the old snapshot. Git review remains the trust
    boundary for coordinated changes to metadata, payload or this updater.
    """
    source = Path(source_dir).absolute()

    if source.resolve() != source or source.is_symlink() or not source.is_dir():
        raise ValueError(f"Cannot verify acquisition source {source}: require a real directory without symlink parents.")

    manifest_path = source / "acquisition-manifest.json"
    (data, metadata) = _regular_bytes(manifest_path)
    manifest_execute_bits = metadata.st_mode & 0o111

    if manifest_execute_bits:
        raise ValueError(f"Acquisition {manifest_path=}: {manifest_execute_bits=:03o}")

    manifest_link_count = metadata.st_nlink

    if manifest_link_count != 1:
        raise ValueError(f"Acquisition {manifest_path=}: {manifest_link_count=} ≠ 1")

    manifest = read_manifest(data)
    entries = manifest["payload_files"]

    expected_dirs = {
        parent.as_posix() for name in entries
        for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")
    }

    actual = set()

    for (root, directories, files) in os.walk(source, followlinks=False, onerror=_raise_walk_error):
        for name in directories:
            directory = Path(root) / name

            if directory.is_symlink() or directory.relative_to(source).as_posix() not in expected_dirs:
                raise ValueError(f"Acquisition source contains an unrecorded or symlink directory: {directory}.")

        for name in files:
            path = Path(root) / name
            acquisition_rel_path = path.relative_to(source).as_posix()

            if acquisition_rel_path == "acquisition-manifest.json":
                actual.add(acquisition_rel_path)
                continue

            if not (acquisition_rel_path in entries):
                raise ValueError(f"Acquisition source contains an unrecorded file: {acquisition_rel_path=}")

            (content, metadata) = _regular_bytes(path)

            acquisition_link_count = metadata.st_nlink

            if acquisition_link_count != 1:
                raise ValueError(f"Acquisition {acquisition_rel_path=}: {acquisition_link_count=} ≠ 1")

            _check_bytes(acquisition_rel_path, content, entries[acquisition_rel_path])

            # Git records executable bits, not checkout read/write permissions.
            actual_execute_bits = metadata.st_mode & 0o111
            release_execute_bits = entries[acquisition_rel_path]["mode"] & 0o111

            if actual_execute_bits != release_execute_bits:
                raise ValueError(
                    f"Acquisition {acquisition_rel_path=}: {actual_execute_bits=:03o} ≠ {release_execute_bits=:03o}"
                )

            actual.add(acquisition_rel_path)

    unobserved_files = sorted((set(entries) | {"acquisition-manifest.json"}) - actual)

    if unobserved_files:
        raise ValueError(f"Acquisition source did not contain {unobserved_files=}")

    return manifest


def _admit_archive(
    *,
    archive_bytes: bytes,
    expected_archive_sha256: str,
) -> tuple[dict[str, bytes], AcquisitionManifest]:
    """Authenticate the exact archive before reading its manifest or payload.

    The trusted digest identifies the archive; its embedded release version
    describes those authenticated bytes and needs no separate comparison.
    """

    # The manifest is inside the download. It cannot authenticate that download.
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    if archive_sha256 != expected_archive_sha256:
        raise ValueError(f"Acquisition archive checksum mismatch: {archive_sha256=} ≠ {expected_archive_sha256=}")

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        members = archive.getmembers()
        member_counts = Counter(member.name for member in members)

        if duplicate_members := member_counts - Counter(member_counts.keys()):
            raise ValueError(f"Acquisition archive contains {duplicate_members=}")

        for member in members:
            path = _relative_name(member.name)

            for parent in path.parents:
                if parent.as_posix() in member_counts:
                    raise ValueError(f"Acquisition archive: {member.name=} has an archived {parent=}")

            if not member.isfile():
                raise ValueError(f"Acquisition archive: {member.name=} is not a file: {member.type=}")

        if member_counts["acquisition-manifest.json"] != 1:
            raise ValueError("Acquisition archive must contain one acquisition-manifest.json.")

        manifest_member = archive.getmember("acquisition-manifest.json")

        if manifest_member.mode != 0o644:
            raise ValueError(f"Acquisition archive manifest {manifest_member.mode=:04o} ≠ 0644")

        payload = {member.name: archive.extractfile(member).read() for member in members}
        manifest = read_manifest(payload["acquisition-manifest.json"])

        entries = manifest["payload_files"]

        if set(payload) != set(entries) | {"acquisition-manifest.json"}:
            raise ValueError("Acquisition archive members differ from its embedded inventory.")

        for member in members:
            if member.name == "acquisition-manifest.json":
                continue

            entry = entries[member.name]
            expected_size = entry["size"]
            expected_mode = entry["mode"]

            if member.size != expected_size:
                raise ValueError(f"Acquisition archive {member.name=}: {member.size=} ≠ {expected_size=}")

            if member.mode != expected_mode:
                raise ValueError(f"Acquisition archive {member.name=}: {member.mode=:o} ≠ {expected_mode=:o}")

            _check_bytes(member.name, payload[member.name], entry)

    return (payload, manifest)


def _exchange_directories(first, second):
    """Use the existing Linux directory-exchange primitive from SECS bootstrap."""
    libc = ctypes.CDLL(None, use_errno=True)
    exchange = libc.renameat2
    exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    exchange.restype = ctypes.c_int

    if exchange(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        number = ctypes.get_errno()

        raise OSError(number, os.strerror(number), f"{first} <-> {second}")


def _publish(source, stage):
    # Everything is verified before this single publication operation. Code
    # and its manifest change together; the consumer's pin is never rewritten.
    try:
        _exchange_directories(source, stage)
    except BaseException as error:
        error.add_note(f"Snapshot exchange did not complete normally; retain and inspect {source} and {stage}.")
        raise

    try:
        shutil.rmtree(stage)
    except BaseException as error:
        error.add_note(f"New acquisition source is published at {source}; previous snapshot cleanup failed at {stage}.")
        raise


def _import_verified_old(source, pin, archive):
    (archive_bytes, _metadata) = _regular_bytes(Path(archive))

    (payload, manifest) = _admit_archive(
        archive_bytes=archive_bytes,
        expected_archive_sha256=pin["archive_sha256"],
    )

    stage = Path(tempfile.mkdtemp(prefix=".acquisition-next-", dir=source.parent))

    try:
        manifest_path = stage / "acquisition-manifest.json"

        with manifest_path.open("xb") as stream:
            stream.write(payload["acquisition-manifest.json"])

        manifest_path.chmod(0o644)

        for (name, entry) in manifest["payload_files"].items():
            path = stage / name

            path.parent.mkdir(parents=True, exist_ok=True)

            with path.open("xb") as stream:
                stream.write(payload[name])

            path.chmod(entry["mode"])

        for (root, _directories, _files) in os.walk(stage):
            Path(root).chmod(0o755)

        verify(stage)

        # A download or staging can take time. Refuse an old-source edit made
        # while this command was preparing the replacement, before exchanging.
        verify(source)
    except BaseException as primary:
        try:
            shutil.rmtree(stage)
        except OSError as cleanup:
            raise BaseExceptionGroup("Acquisition staging failed and its cleanup also failed", [primary, cleanup])

        raise

    _publish(source, stage)

    return manifest


def import_archive(source_dir, pin_file, archive):
    """Replace clean old source from an authenticated local archive, offline."""
    source = Path(source_dir).absolute()

    verify(source)

    return _import_verified_old(source, read_pin(pin_file), archive)


def update(source_dir, pin_file):
    """Download the one pinned GitHub asset and use the same import boundary."""
    source = Path(source_dir).absolute()

    verify(source)
    pin = read_pin(pin_file)
    name = f"acquisition-owner-{pin['version']}.tar.gz"
    published = False

    try:
        with tempfile.TemporaryDirectory(prefix="provider-tools-release-") as temporary:
            subprocess.run([
                "gh",
                "release",
                "download",
                f"v{pin['version']}",
                "--repo",
                "numpde/nmr-provider-tools",
                "--dir",
                temporary,
                "--pattern",
                name,
            ],
                check=True)

            manifest = _import_verified_old(source, pin, Path(temporary) / name)
            published = True
    except BaseException as error:
        if published:
            error.add_note(f"Acquisition source was published at {source}; release-download cleanup failed.")

        raise

    return manifest


def main():
    """Expose source verification, pinned remote update and offline import."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)

    for operation in ("verify", "update", "import"):
        command = commands.add_parser(operation)

        command.add_argument("--source-dir", type=Path, required=True)

        if operation != "verify":
            command.add_argument("--pin-file", type=Path, required=True)

        if operation == "import":
            command.add_argument("--archive", type=Path, required=True)

    args = parser.parse_args()

    if args.operation == "verify":
        verify(args.source_dir)
    elif args.operation == "update":
        update(args.source_dir, args.pin_file)
    else:
        import_archive(args.source_dir, args.pin_file, args.archive)


if __name__ == "__main__":
    main()
