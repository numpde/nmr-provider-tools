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
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from tools.artifacts import fsync_directory, regular_file

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
    current = None
    with regular_file(lock) as stream:
        for raw in stream:
            line = raw.decode().strip().removesuffix("\\").rstrip()
            if not line or line.startswith("#"):
                continue
            if match := re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", line):
                if current is None:
                    raise ValueError("Cannot acquire wheels: hash precedes its locked package")
                packages[current].add(match[1])
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)", line)
            if match is None:
                raise ValueError("Cannot acquire wheels: lock entries must contain only named pinned versions and SHA-256 hashes.")
            current = (re.sub(r"[-_.]+", "-", match[1]).lower(), match[2])
            if current in packages:
                raise ValueError(f"Cannot acquire wheels: duplicate locked package {current}")
            packages[current] = set()
    if not packages or not all(packages.values()):
        raise ValueError("Cannot acquire wheels: every locked package must have SHA-256 hashes")
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
        frozen = Path(temporary) / "requirements.lock"
        with regular_file(lock) as source, frozen.open("xb") as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)
        requirements(frozen)
        report = Path(temporary) / "report.json"
        borrowed_descriptors = ()
        if lock_fd is not None:
            borrowed_descriptors = (lock_fd,)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--quiet",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                *[argument for directory in pools for argument in ("--find-links", str(directory))],
                "--report",
                str(report),
                "-r",
                str(frozen),
            ],
            check=True, pass_fds=borrowed_descriptors,
        )
        return wheel_inputs_from_report(frozen, pool, report, extra_pools, required_files)


# silent-defaults: allow-contract -- Optional additional wheel pools are captured as an immutable tuple; required files are independently checked.
def wheel_inputs_from_report(lock, pool, report, extra_pools=(), required_files=None):
    """Project pip's actual selection or installation report into input identities."""
    selected = read_json(report)
    if selected["version"] != "1":
        raise ValueError("Cannot select wheels: pip returned an unsupported installation-report version.")
    files = {}
    for item in selected["install"]:
        location = urllib.parse.urlsplit(item["download_info"]["url"])
        candidate = Path(urllib.request.url2pathname(location.path))
        if (
            location.scheme != "file"
            or location.netloc
            or candidate.parent.resolve() not in {directory.resolve() for directory in (pool, *extra_pools)}
            or candidate.suffix != ".whl"
        ):
            raise ValueError("Cannot select wheels: pip selected a file outside the supplied flat pool.")
        if required_files and candidate.name in required_files and candidate.resolve() != required_files[candidate.name].resolve():
            raise ValueError(f"Cannot select {candidate.name}: pip did not select the required release artifact.")
        with regular_file(candidate):
            pass
        # Pip owns lock resolution and checks the selected file against the lock.
        files[candidate.name] = item["download_info"]["archive_info"]["hashes"]["sha256"]
    if required_files and not set(required_files).issubset(files):
        raise ValueError("Cannot select wheels: a required release artifact was not selected.")
    return {"lock_sha256": digest(lock), "files": files}


def publish_file(path, destination, checksum):
    """Add one complete file without replacing a peer's existing inode or bytes."""
    with regular_file(path) as stream:
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())
    try:
        os.link(path, destination, follow_symlinks=False)
    except FileExistsError as conflict:
        try:
            observed = digest(destination)
            if observed != checksum:
                raise ValueError(
                    f"Cannot reuse {destination.name!r}: expected SHA-256 {checksum}, found {observed}; the existing file is preserved."
                )
        except (OSError, ValueError) as invalid_winner:
            raise conflict from invalid_winner
    fsync_directory(destination.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check",))
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--lock", type=Path, action="append", required=True)
    args = parser.parse_args()
    for lock in args.lock:
        with pool_read_lock(args.pool) as lock_fd:
            print(json.dumps(select_wheels(lock, args.pool, lock_fd=lock_fd), sort_keys=True))


if __name__ == "__main__":
    main()
