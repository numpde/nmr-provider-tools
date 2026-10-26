"""Select and freeze hash-locked wheels from one additive shared pool.

Ported from nmrnet-repro d09b77b951551c977e36bc87b5822315a4d1e944,
tools/wheelhouse.py. Scientific source handling stays with its provider.
"""

from tools.container_guard import require_container

require_container()

import argparse
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
from pathlib import Path

from tools.artifacts import fsync_directory, regular_file, require_download_proxy

# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024


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


def select_wheels(lock, pool):
    """Let pip select compatible, hash-locked files from an unrelated shared pool.

    A dry-run report avoids copying large wheels into the checker's small /tmp.
    No package is installed; --ignore-installed makes the pool prove every pin.
    https://pip.pypa.io/en/stable/cli/pip_install/#obtaining-information-about-what-was-installed
    https://pip.pypa.io/en/stable/reference/installation-report/
    """
    if not pool.is_dir():
        raise ValueError(f"Cannot select wheels: shared pool is not a directory: {pool}")
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
                "--find-links",
                str(pool),
                "--report",
                str(report),
                "-r",
                str(frozen),
            ],
            check=True,
        )
        return wheel_inputs_from_report(frozen, pool, report)


def wheel_inputs_from_report(lock, pool, report):
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
            or candidate.parent.resolve() != pool.resolve()
            or candidate.suffix != ".whl"
        ):
            raise ValueError("Cannot select wheels: pip selected a file outside the supplied flat pool.")
        # Pip owns lock resolution and source-file hash admission. Capture later
        # compares its copied bytes with these hashes and the original lock.
        files[candidate.name] = item["download_info"]["archive_info"]["hashes"]["sha256"]
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


def capture(lock, pool, destination):
    """Freeze selected bytes and reject changes between selection and copying."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Cannot capture wheels: destination must be a private empty directory.")
    selected = select_wheels(lock, pool)
    for name in selected["files"]:
        with regular_file(pool / name) as source, (destination / name).open("xb") as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)
            os.fchmod(target.fileno(), 0o644)
            target.flush()
            os.fsync(target.fileno())
    if {name: digest(destination / name) for name in selected["files"]} != selected["files"]:
        raise ValueError("Cannot capture wheels: selected bytes changed while copying.")
    fsync_directory(destination)
    return selected


def populate_pool(lock, pool):
    """Add verified wheels without replacing a peer's existing cache files."""
    require_download_proxy()
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError("Cannot acquire wheels: shared pool must be a directory without a symlink.")
    stage = Path(tempfile.mkdtemp(prefix=".provider-core-download-", dir=pool))
    frozen = stage / "requirements.lock"
    with regular_file(lock) as source, frozen.open("xb") as target:
        shutil.copyfileobj(source, target, CHUNK_BYTES)
    requirements(frozen)
    print(f"Downloading locked wheels in {stage}; failed staging is retained.", flush=True)
    subprocess.run(
        [sys.executable, "-I", "-m", "pip", "--isolated", "download", "--require-hashes",
         "--only-binary=:all:", "--no-deps", "--cache-dir", str(pool / ".pip-cache"),
         "--disable-pip-version-check", "--index-url", "https://pypi.org/simple",
         "--find-links", str(pool), "--dest", str(stage), "-r", str(frozen)],
        check=True,
    )
    selected = select_wheels(frozen, stage)
    for name, checksum in selected["files"].items():
        publish_file(stage / name, pool / name, checksum)
    shutil.rmtree(stage)
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
            print(json.dumps(select_wheels(lock, args.pool), sort_keys=True))


if __name__ == "__main__":
    main()
