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

from tools.artifacts import fsync_directory, regular_file, require_download_proxy

# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024


@contextmanager
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
            check=True, pass_fds=() if lock_fd is None else (lock_fd,),
        )
        return wheel_inputs_from_report(frozen, pool, report, extra_pools, required_files)


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


def populate_pool(lock, pool):
    """Reuse the shared pool and download missing hash-locked wheels into it."""
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError("Cannot acquire wheels: shared pool must be a directory without a symlink.")
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _populate_pool(lock, pool, descriptor)
    finally:
        os.close(descriptor)


def _populate_pool(lock, pool, lock_fd):
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from pip._vendor.packaging.version import Version

    locked = {(name, Version(version)): hashes for (name, version), hashes in requirements(lock).items()}
    try:
        select_wheels(lock, pool, lock_fd=lock_fd)
    except subprocess.CalledProcessError:
        pass
    else:
        print("All locked wheels are already available in WHEELHOUSE; no download is needed.", flush=True)
        return
    target_tags = set(sys_tags())
    for path in pool.glob("*.whl"):
        try:
            name, version, _build, tags = parse_wheel_filename(path.name)
        except InvalidWheelFilename:
            continue
        hashes = locked.get((name, version))
        if hashes and tags.intersection(target_tags) and digest(path) not in hashes:
            raise ValueError(f"Cannot acquire {path.name}: cached bytes differ from the lock; the existing file is preserved.")
    require_download_proxy()
    print("Acquiring missing locked wheels directly into WHEELHOUSE.", flush=True)
    subprocess.run(
        [sys.executable, "-I", "-m", "pip", "--isolated", "download", "--require-hashes",
         "--only-binary=:all:", "--no-deps", "--no-cache-dir",
         "--disable-pip-version-check", "--index-url", "https://pypi.org/simple",
         "--find-links", str(pool), "--dest", str(pool), "-r", str(lock)],
        check=True, pass_fds=(lock_fd,),
    )
    select_wheels(lock, pool, lock_fd=lock_fd)
    fsync_directory(pool)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check"))
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--lock", type=Path, action="append", required=True)
    args = parser.parse_args()
    for lock in args.lock:
        if args.action == "download":
            populate_pool(lock, args.pool)
        else:
            with pool_read_lock(args.pool) as lock_fd:
                print(json.dumps(select_wheels(lock, args.pool, lock_fd=lock_fd), sort_keys=True))


if __name__ == "__main__":
    main()
