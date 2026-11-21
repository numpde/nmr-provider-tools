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
import pathlib
from pathlib import PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import TypedDict

if __name__ == "__main__":
    # Issued bundles keep the helper at their root
    # Direct isolated execution must expose that root before importing it
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from type_requirements import require_type


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


def _version(release_version):
    return (type(release_version) is str) and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", release_version)


def is_checksum(candidate_checksum):
    return (type(candidate_checksum) is str) and (re.fullmatch(r"[0-9a-f]{64}", candidate_checksum) is not None)


def read_pin(release_pin_path):
    """Read the consumer's expected release; downloads never rewrite this pin."""
    (pin_bytes, _) = _regular_bytes(pathlib.Path(release_pin_path))
    pin = json.loads(pin_bytes)

    require_type(pin, dict, error_factory=lambda diagnostic: TypeError(f"{release_pin_path=}; " + diagnostic))

    if set(pin) != {"version", "archive_sha256"}:
        raise ValueError(f"Acquisition {release_pin_path=}: require exactly version and archive_sha256; {sorted(pin)=}")

    release_version = pin["version"]

    if not _version(release_version):
        raise ValueError(f"Acquisition {release_pin_path=}: {release_version=} must be X.Y.Z in ASCII digits without leading zeroes")

    if not is_checksum(pin["archive_sha256"]):
        raise ValueError(f"Acquisition {release_pin_path=}: archive_sha256 must have 64 lowercase hexadecimal characters")

    return pin


def _relative_name(acquisition_path):
    require_type(acquisition_path, str)

    if (not acquisition_path) or ("\0" in acquisition_path):
        raise ValueError(f"Acquisition path must be nonempty and contain no nulls: {acquisition_path=}")

    posix_path = PurePosixPath(acquisition_path)

    is_canonical_relative_path = (
        bool(posix_path.parts) and
        (not posix_path.is_absolute()) and
        (".." not in posix_path.parts) and
        (posix_path.as_posix() == acquisition_path)
    )

    if not is_canonical_relative_path:
        raise ValueError(f"Acquisition path is not canonical and relative: {acquisition_path=}")

    return posix_path


def read_manifest(manifest_json: str | bytes | bytearray) -> AcquisitionManifest:
    """Admit the recorded inventory, excluding only the manifest's own bytes."""
    manifest = json.loads(manifest_json)

    require_type(manifest, dict)

    required_manifest_fields = {
        "release_version",
        "source_revision",
        "payload_files",
        "dependency_inputs",
    }

    missing_manifest_fields = sorted(required_manifest_fields - set(manifest))
    unexpected_manifest_fields = sorted(set(manifest) - required_manifest_fields)

    if missing_manifest_fields or unexpected_manifest_fields:
        raise ValueError(f"Acquisition manifest {missing_manifest_fields=}; {unexpected_manifest_fields=}")

    manifest_release_version = manifest["release_version"]

    if not _version(manifest_release_version):
        raise ValueError(f"Acquisition manifest {manifest_release_version=} must be X.Y.Z in ASCII digits without leading zeroes")

    require_type((manifest_source_revision := manifest["source_revision"]), str)

    if not re.fullmatch(r"[0-9a-f]{40}", manifest_source_revision):
        raise ValueError(f"Acquisition manifest revision must be 40 lowercase hexadecimal characters: {manifest_source_revision=}")

    payload_files = manifest["payload_files"]

    require_type(payload_files, dict)

    if not payload_files:
        raise ValueError(f"{payload_files=} should be nonempty")

    if "acquisition-manifest.json" in payload_files:
        raise ValueError(f"{'acquisition-manifest.json' in payload_files=} should be False")

    for (acquisition_rel_path, entry) in payload_files.items():
        inventory_path = _relative_name(acquisition_rel_path)

        for parent in inventory_path.parents:
            if parent.as_posix() in payload_files:
                raise ValueError(f"Acquisition {acquisition_rel_path=} has an inventoried {parent=}")

        require_type(entry, dict, error_factory=lambda diagnostic: TypeError(f"{acquisition_rel_path=}; " + diagnostic))

        if set(entry) != {"sha256", "size", "mode"}:
            raise ValueError(f"Acquisition {acquisition_rel_path=}: metadata must contain exactly sha256, size and mode; {sorted(entry)=}")

        if not is_checksum(entry["sha256"]):
            raise ValueError(f"Acquisition {acquisition_rel_path=}: sha256 must have 64 lowercase hexadecimal characters")

        require_type((entry_size := entry["size"]), int, error_factory=lambda diagnostic: TypeError(f"{acquisition_rel_path=}; " + diagnostic))

        if not (0 <= entry["size"]):
            raise ValueError(f"Acquisition {acquisition_rel_path=}: {entry_size=} must be nonnegative")

        require_type((entry_mode := entry["mode"]), int, error_factory=lambda diagnostic: TypeError(f"{acquisition_rel_path=}; " + diagnostic))

        if not (entry["mode"] in (0o644, 0o755)):
            raise ValueError(f"Acquisition {acquisition_rel_path=}: {entry_mode=:o} (octal); JSON mode must be 420 or 493")

    dependencies = manifest["dependency_inputs"]

    require_type(dependencies, dict)

    for (dependency_name, dependency_sha256) in dependencies.items():
        require_type(dependency_name, str)

        require_type(dependency_sha256, str, error_factory=lambda diagnostic: TypeError(f"{dependency_name=}; " + diagnostic))

        if not dependency_name:
            raise ValueError(f"Acquisition dependency name must be nonempty: {dependency_name=}")

        if not is_checksum(dependency_sha256):
            raise ValueError(f"Acquisition {dependency_name=}: {dependency_sha256=} must be a lowercase SHA-256 digest")

    return manifest


def _regular_bytes(acquisition_input_path):
    """Read one opened regular file, without following links or blocking on a FIFO."""
    descriptor = os.open(acquisition_input_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    try:
        metadata = os.fstat(descriptor)

        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Cannot read {acquisition_input_path=}: require a regular file")

        with os.fdopen(descriptor, "rb", closefd=False) as acquisition_file:
            return (acquisition_file.read(), metadata)
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
    source = pathlib.Path(source_dir).absolute()

    if (source.resolve() != source) or (source.is_symlink()) or (not source.is_dir()):
        raise ValueError(f"{source=} should be a real directory without symlink parents")

    manifest_path = source / "acquisition-manifest.json"
    (manifest_bytes, metadata) = _regular_bytes(manifest_path)
    manifest_execute_bits = metadata.st_mode & 0o111

    if manifest_execute_bits:
        raise ValueError(f"Acquisition {manifest_path=}: {manifest_execute_bits=:03o} ≠ 000 (octal)")

    manifest_link_count = metadata.st_nlink

    if manifest_link_count != 1:
        raise ValueError(f"Acquisition {manifest_path=}: {manifest_link_count=} ≠ 1")

    manifest = read_manifest(manifest_bytes)
    payload_files = manifest["payload_files"]

    expected_dirs = {
        parent.as_posix() for name in payload_files
        for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")
    }

    actual = set()

    for (root, directories, files) in os.walk(source, followlinks=False, onerror=_raise_walk_error):
        for name in directories:
            acquisition_directory = pathlib.Path(root) / name

            if acquisition_directory.is_symlink():
                raise ValueError(f"Acquisition directory is a symlink: {acquisition_directory=}")

            acquisition_rel_directory = acquisition_directory.relative_to(source).as_posix()

            if not (acquisition_rel_directory in expected_dirs):
                raise ValueError(f"Acquisition source has an unrecorded {acquisition_rel_directory=}")

        for name in files:
            acquisition_file_path = pathlib.Path(root) / name
            acquisition_rel_path = acquisition_file_path.relative_to(source).as_posix()

            if acquisition_rel_path == "acquisition-manifest.json":
                actual.add(acquisition_rel_path)
                continue

            if not (acquisition_rel_path in payload_files):
                raise ValueError(f"Acquisition source contains an unrecorded file: {acquisition_rel_path=}")

            (content, metadata) = _regular_bytes(acquisition_file_path)

            acquisition_link_count = metadata.st_nlink

            if acquisition_link_count != 1:
                raise ValueError(f"Acquisition {acquisition_rel_path=}: {acquisition_link_count=} ≠ 1")

            _check_bytes(acquisition_rel_path, content, payload_files[acquisition_rel_path])

            # Git records executable bits, not checkout read/write permissions.
            actual_execute_bits = metadata.st_mode & 0o111

            release_execute_bits = payload_files[acquisition_rel_path]["mode"] & 0o111

            if actual_execute_bits != release_execute_bits:
                raise ValueError(f"Acquisition {acquisition_rel_path=}: {actual_execute_bits=:03o} ≠ {release_execute_bits=:03o} (octal)")

            actual.add(acquisition_rel_path)

    unobserved_files = sorted((set(payload_files) | {"acquisition-manifest.json"}) - actual)

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

        if extra_member_occurrences := member_counts - Counter(member_counts.keys()):
            raise ValueError(f"Acquisition archive contains {extra_member_occurrences=}")

        for member in members:
            archive_member_path = _relative_name(member.name)

            for parent in archive_member_path.parents:
                if parent.as_posix() in member_counts:
                    raise ValueError(f"Acquisition archive: {member.name=} has an archived {parent=}")

            if not member.isfile():
                raise ValueError(f"Acquisition archive: {member.name=} is not a file: {member.type=}")

        if member_counts["acquisition-manifest.json"] != 1:
            raise ValueError(f"{member_counts['acquisition-manifest.json']=} should be one")

        manifest_member = archive.getmember("acquisition-manifest.json")

        if manifest_member.mode != 0o644:
            raise ValueError(f"Acquisition archive manifest {manifest_member.mode=:04o} ≠ 0644 (octal)")

        payload = {member.name: archive.extractfile(member).read() for member in members}
        manifest = read_manifest(payload["acquisition-manifest.json"])

        payload_files = manifest["payload_files"]

        expected_members = set(payload_files) | {"acquisition-manifest.json"}
        missing_members = sorted(expected_members - set(payload))

        if missing_members:
            raise ValueError(f"Acquisition archive is missing {missing_members=}")

        unrecorded_members = sorted(set(payload) - expected_members)

        if unrecorded_members:
            raise ValueError(f"Acquisition archive contains {unrecorded_members=}")

        for member in members:
            if member.name == "acquisition-manifest.json":
                continue

            entry = payload_files[member.name]
            expected_size = entry["size"]
            expected_mode = entry["mode"]

            if member.size != expected_size:
                raise ValueError(f"Acquisition archive {member.name=}: {member.size=} ≠ {expected_size=}")

            if member.mode != expected_mode:
                raise ValueError(f"Acquisition archive {member.name=}: {member.mode=:o} ≠ {expected_mode=:o} (octal)")

            _check_bytes(member.name, payload[member.name], entry)

    return (payload, manifest)


def _exchange_directories(source_directory, replacement_directory):
    """Use the existing Linux directory-exchange primitive from SECS bootstrap."""
    libc = ctypes.CDLL(None, use_errno=True)
    exchange = libc.renameat2
    exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    exchange.restype = ctypes.c_int

    # AT_FDCWD is -100; RENAME_EXCHANGE (2) swaps both directory entries atomically
    if exchange(-100, os.fsencode(source_directory), -100, os.fsencode(replacement_directory), 2) != 0:
        number = ctypes.get_errno()

        raise OSError(number, os.strerror(number), os.fspath(source_directory), None, os.fspath(replacement_directory))


def _publish(source, stage):
    # Everything is verified before this single publication operation.
    # Code and its manifest change together; the consumer's pin is never rewritten.
    try:
        _exchange_directories(source, stage)
    except BaseException as error:
        error.add_note(f"Snapshot exchange did not complete normally; retain and inspect {source=} and {stage=}")
        raise

    try:
        shutil.rmtree(stage)
    except BaseException as error:
        error.add_note(f"New acquisition {source=} is published; previous snapshot cleanup failed at {stage=}")
        raise


def _import_verified_old(source, pin, archive):
    (archive_bytes, _metadata) = _regular_bytes(pathlib.Path(archive))

    (payload, manifest) = _admit_archive(
        archive_bytes=archive_bytes,
        expected_archive_sha256=pin["archive_sha256"],
    )

    stage = pathlib.Path(tempfile.mkdtemp(prefix=".acquisition-next-", dir=source.parent))

    try:
        manifest_path = stage / "acquisition-manifest.json"

        with manifest_path.open("xb") as stream:
            stream.write(payload["acquisition-manifest.json"])

        manifest_path.chmod(0o644)

        for (name, entry) in manifest["payload_files"].items():
            staged_file = stage / name

            staged_file.parent.mkdir(parents=True, exist_ok=True)

            with staged_file.open("xb") as stream:
                stream.write(payload[name])

            staged_file.chmod(entry["mode"])

        for (root, _directories, _files) in os.walk(stage):
            pathlib.Path(root).chmod(0o755)

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
    source = pathlib.Path(source_dir).absolute()

    verify(source)

    return _import_verified_old(source, read_pin(pin_file), archive)


def main():
    """Expose offline source verification and authenticated archive import."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)

    for operation in ("verify", "import"):
        command = commands.add_parser(operation)

        command.add_argument("--source-dir", type=pathlib.Path, required=True)

        if operation != "verify":
            command.add_argument("--pin-file", type=pathlib.Path, required=True)

        if operation == "import":
            command.add_argument("--archive", type=pathlib.Path, required=True)

    args = parser.parse_args()

    if args.operation == "verify":
        verify(args.source_dir)
    else:
        import_archive(args.source_dir, args.pin_file, args.archive)


if __name__ == "__main__":
    main()
