"""Select and acquire hash-locked wheels in one additive shared pool.

Ported from nmrnet-repro d09b77b951551c977e36bc87b5822315a4d1e944,
tools/wheelhouse.py. Scientific source handling stays with its provider.
"""

from tools.container_guard import require_container

require_container()

import argparse
import fcntl
import hashlib
import json
import os
import reprlib
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import pathlib
from contextlib import contextmanager

from tools.adapters.pip_install import resolve_locked_wheels_command
from tools.artifacts import fsync_directory, regular_file
from tools.validation.upstream import pip_installation_report
from tools.validation.upstream.pip_requirements import parse_lock_line

# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024


@contextmanager
# silent-defaults: allow-contract -- Shared read-only cache access is the default; exclusive acquisition is explicitly requested by the caller.
def pool_lock(pool, exclusive=False):
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


def pool_read_lock(pool):
    return pool_lock(pool)


def digest(path):
    checksum = hashlib.sha256()

    with regular_file(path) as stream:
        while chunk := stream.read(CHUNK_BYTES):
            checksum.update(chunk)

    return checksum.hexdigest()


def read_json(path):
    with regular_file(path) as stream:
        return json.load(stream)


def requirements(lock):
    """Read the fixed name/version and allowed SHA-256 values emitted by our lock lane."""
    packages = {}
    package_identity = None

    with regular_file(lock) as stream:
        for raw in stream:
            pin_or_hash = parse_lock_line(raw)

            if pin_or_hash is None:
                continue

            if isinstance(pin_or_hash, str):
                if package_identity is None:
                    raise ValueError(f"{pin_or_hash=} should follow a pinned requirement")

                packages[package_identity].add(pin_or_hash)
                continue

            package_identity = pin_or_hash

            if package_identity in packages:
                raise ValueError(f"{reprlib.repr(package_identity)=} should occur only once")

            packages[package_identity] = set()

    if not packages:
        raise ValueError(f"{lock=} should contain pinned requirements")

    if unhashed_packages := [identity for (identity, hashes) in packages.items() if not hashes]:
        raise ValueError(f"{reprlib.repr(unhashed_packages)=} should have SHA-256 hashes")

    return packages


# silent-defaults: allow-contract -- Extra pools are immutable empty by default; required files and lock descriptors remain explicit optional authorities.
def select_wheels(lock, pool, extra_pools=(), required_files=None, lock_fd=None):
    """Let pip select compatible, hash-locked files from an unrelated shared pool.

    A dry-run report avoids copying large wheels into the checker's small /tmp.
    No package is installed; --ignore-installed makes the pool prove every pin.
    https://pip.pypa.io/en/stable/cli/pip_install/#obtaining-information-about-what-was-installed
    https://pip.pypa.io/en/stable/reference/installation-report/
    """
    pools = (pool, *extra_pools)

    if any(not directory.is_dir() for directory in pools):
        raise ValueError("Cannot select wheels: every supplied wheel directory must exist.")

    with tempfile.TemporaryDirectory(prefix="wheel-selection-") as temporary:
        frozen = pathlib.Path(temporary) / "requirements.lock"

        with regular_file(lock) as source, frozen.open("xb") as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)

        requirements(frozen)
        report = pathlib.Path(temporary) / "report.json"
        borrowed_descriptors = ()

        if lock_fd is not None:
            borrowed_descriptors = (lock_fd,)

        subprocess.run(
            resolve_locked_wheels_command(sys.executable, frozen, pools, report),
            check=True,  # Stop if resolution fails
            pass_fds=borrowed_descriptors,  # Keep shared pool protection alive in pip
        )

        return wheel_inputs_from_report(frozen, pool, report, extra_pools, required_files)


# silent-defaults: allow-contract -- Optional additional wheel pools are captured as an immutable tuple; required files are independently checked.
def wheel_inputs_from_report(lock, pool, report, extra_pools=(), required_files=None):
    """Project pip's actual selection or installation report into input identities."""
    installed_distributions = pip_installation_report.install_records(read_json(report))
    files = {}

    for installed_distribution in installed_distributions:
        location = urllib.parse.urlsplit(pip_installation_report.artifact_url(installed_distribution))
        candidate = pathlib.Path(urllib.request.url2pathname(location.path))

        if (
            (location.scheme != "file") or
            location.netloc or
            (candidate.parent.resolve() not in {directory.resolve() for directory in (pool, *extra_pools)}) or
            (candidate.suffix != ".whl")
        ):
            raise ValueError("Cannot select wheels: pip selected a file outside the supplied flat pool.")

        if required_files and (candidate.name in required_files):
            selected_artifact = candidate.resolve()
            required_release_artifact = required_files[candidate.name].resolve()

            if not (selected_artifact == required_release_artifact):
                raise ValueError(f"{selected_artifact=} should match {required_release_artifact=}")

        with regular_file(candidate):
            pass

        # Pip owns lock resolution and checks the selected file against the lock.
        files[candidate.name] = pip_installation_report.archive_sha256(installed_distribution)

    if (required_files) and (not set(required_files).issubset(files)):
        raise ValueError("Cannot select wheels: a required release artifact was not selected.")

    return {"lock_sha256": digest(lock), "files": files}


def publish_file(path, destination, expected_sha256):
    """Add one complete file without replacing a peer's existing inode or bytes."""

    with regular_file(path) as stream:
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())

    try:
        os.link(path, destination, follow_symlinks=False)
    except FileExistsError as conflict:
        try:
            existing_sha256 = digest(destination)

            if not (existing_sha256 == expected_sha256):
                artifact_name = destination.name

                raise ValueError(f"For {artifact_name=}, {existing_sha256=} should match {expected_sha256=}. The existing file was retained.")
        except (OSError, ValueError) as invalid_winner:
            raise conflict from invalid_winner

    fsync_directory(destination.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("check",))
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--lock", type=pathlib.Path, action="append", required=True)
    args = parser.parse_args()

    for lock in args.lock:
        with pool_read_lock(args.pool) as lock_fd:
            print(json.dumps(select_wheels(lock, args.pool, lock_fd=lock_fd), sort_keys=True))


if __name__ == "__main__":
    main()
