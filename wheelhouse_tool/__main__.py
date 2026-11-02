"""Acquire or check the artifacts selected by a target's pylock file."""

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from packaging.pylock import PackageArchive, PackageSdist, PackageWheel, Pylock, PylockSelectError, PylockValidationError
from packaging.tags import sys_tags
from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, canonicalize_name, parse_sdist_filename, parse_wheel_filename


@contextmanager
def pool_lock(pool, exclusive):
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


def sha256(path, directory_fd=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Cannot use {Path(path).name}: expected a regular file.")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_lock(lock):
    with lock.open("rb") as stream:
        return Pylock.from_dict(tomllib.load(stream))


def selected(lock, prefer_sdist, require_sdist=(), document=None):
    if document is None:
        document = read_lock(lock)
    required = {canonicalize_name(name) for name in require_sdist}
    preferences = {canonicalize_name(name) for name in prefer_sdist} | required
    artifacts = []
    seen = {}
    found_required = set()
    for package, artifact in document.select(prefer_sdist_predicate=lambda name: name in preferences):
        if not isinstance(artifact, (PackageWheel, PackageSdist, PackageArchive)) or artifact.url is None:
            raise ValueError(f"Cannot acquire {package.name}: expected a URL for a locked artifact.")
        url = urllib.parse.urlsplit(artifact.url)
        name = getattr(artifact, "filename", None) or urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
        digest = artifact.hashes.get("sha256", "")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"Cannot acquire {package.name}: the artifact filename is invalid.")
        source = isinstance(artifact, PackageSdist) or (isinstance(artifact, PackageArchive) and not name.endswith(".whl"))
        if isinstance(artifact, PackageArchive) and not source:
            try:
                wheel_name, wheel_version, _build, wheel_tags = parse_wheel_filename(name)
            except InvalidWheelFilename as error:
                raise ValueError(f"Cannot acquire {package.name}: direct wheel filename is invalid.") from error
            if wheel_name != package.name or (package.version is not None and wheel_version != package.version):
                raise ValueError(f"Cannot acquire {package.name}: direct wheel does not match the locked package.")
            if not wheel_tags.intersection(sys_tags()):
                raise ValueError(f"Cannot acquire {package.name}: direct wheel is incompatible with this target.")
        if source and isinstance(artifact, PackageArchive):
            try:
                source_name, source_version = parse_sdist_filename(name)
            except InvalidSdistFilename as error:
                raise ValueError(f"Cannot acquire {package.name}: direct source archive has no standard distribution filename.") from error
            if source_name != package.name or (package.version is not None and source_version != package.version):
                raise ValueError(f"Cannot acquire {package.name}: direct source archive does not match the locked package.")
        if source and package.name not in preferences:
            raise ValueError(f"Cannot acquire {package.name}: source distributions require an explicit preference.")
        if package.name in required and not source:
            raise ValueError(f"Cannot acquire {package.name}: the lock does not select a source distribution.")
        if package.name in required:
            found_required.add(package.name)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Cannot acquire {name}: a SHA-256 lock hash is required.")
        if url.scheme not in ("https", "http") or not url.netloc or url.username is not None:
            raise ValueError(f"Cannot acquire {name}: expected an HTTP(S) artifact URL without credentials.")
        if name in seen and seen[name] != digest:
            raise ValueError(f"Cannot acquire {name}: the lock selects conflicting bytes for one filename.")
        seen[name] = digest
        artifacts.append((name, digest, artifact.url, package, artifact))
    if missing := required - found_required:
        raise ValueError(f"Cannot acquire source distribution: {', '.join(sorted(missing))} is not selected by the lock.")
    return artifacts


def inspect_artifact(pool, name, digest, directory_fd=None):
    try:
        actual = sha256(name if directory_fd is not None else pool / name, directory_fd)
    except FileNotFoundError:
        return False
    if actual != digest:
        raise ValueError(f"Cannot use {name}: cached bytes differ from the lock; the existing file is preserved.")
    return True


def download(pool, name, digest, url, directory_fd):
    temporary = f".wheelhouse-{secrets.token_hex(16)}.part"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb") as target:
            try:
                source = urllib.request.urlopen(url, timeout=60)
            except (urllib.error.URLError, ValueError) as error:
                raise OSError(f"Cannot acquire {name}: the download request failed.") from error
            with source:
                checksum = hashlib.sha256()
                while True:
                    try:
                        block = source.read(1024 * 1024)
                    except (OSError, http.client.HTTPException) as error:
                        raise OSError(f"Cannot acquire {name}: the download was interrupted.") from error
                    if not block:
                        break
                    checksum.update(block)
                    target.write(block)
            if checksum.hexdigest() != digest:
                raise ValueError(f"Cannot acquire {name}: downloaded bytes differ from the lock.")
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError:
            if not inspect_artifact(pool, name, digest, directory_fd):
                raise FileNotFoundError(f"Cannot acquire {name}: the competing file disappeared.")
        os.fsync(directory_fd)
    finally:
        os.unlink(temporary, dir_fd=directory_fd)


def run(action, lock, pool, prefer_sdist=(), require_sdist=(), artifacts=None):
    if artifacts is None:
        artifacts = selected(lock, prefer_sdist, require_sdist)
    with pool_lock(pool, exclusive=action == "acquire") as directory_fd:
        for name, digest, url, _package, _artifact in artifacts:
            if not inspect_artifact(pool, name, digest, directory_fd):
                if action == "check":
                    raise FileNotFoundError(f"Cannot use {name}: the artifact is missing from the wheelhouse.")
                download(pool, name, digest, url, directory_fd)
    return {"artifacts": [{"package": str(package.name), "filename": name, "sha256": digest}
                          for name, digest, _, package, _ in artifacts]}


def local_lock(lock, local_root, prefer_sdist=(), require_sdist=(), document=None, artifacts=None):
    """Project the selected locked artifacts to their paths in the build."""
    import tomli_w

    if document is None:
        document = read_lock(lock)
    if artifacts is None:
        artifacts = selected(lock, prefer_sdist, require_sdist, document)
    packages = []
    for name, _digest, _url, package, artifact in artifacts:
        local = replace(artifact, url=None, path=str(local_root / name))
        if isinstance(local, PackageWheel):
            package = replace(package, wheels=[local], sdist=None)
        elif isinstance(local, PackageSdist):
            package = replace(package, wheels=None, sdist=local)
        else:
            package = replace(package, archive=local)
        packages.append(package)
    return tomli_w.dumps(replace(document, packages=packages).to_dict())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("acquire", "check", "select"))
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--pool", type=Path)
    parser.add_argument("--prefer-sdist", action="append", default=[])
    parser.add_argument("--require-sdist", action="append", default=[])
    parser.add_argument("--local-lock", type=Path, help="Write a local-path pylock after a successful operation.")
    parser.add_argument("--local-root", type=Path, help="Absolute pool path inside the offline build.")
    args = parser.parse_args()
    try:
        if args.local_lock is not None and (args.local_root is None or not args.local_root.is_absolute()):
            raise ValueError("Cannot write a local lock: supply an absolute --local-root.")
        if args.local_lock is not None and not (args.local_lock.name == "pylock.toml" or
                                               args.local_lock.name.startswith("pylock.") and args.local_lock.name.endswith(".toml")):
            raise ValueError("Cannot write a local lock: use pylock.toml or pylock.NAME.toml so installers recognize it.")
        document = read_lock(args.lock)
        artifacts = selected(args.lock, args.prefer_sdist, args.require_sdist, document)
        if args.action == "select":
            result = {"artifacts": [{"package": str(package.name), "filename": name, "sha256": digest}
                                    for name, digest, _, package, _ in artifacts]}
        else:
            if args.pool is None:
                raise ValueError("Cannot inspect wheelhouse: supply --pool.")
            result = run(args.action, args.lock, args.pool, artifacts=artifacts)
        if args.local_lock is not None:
            output = local_lock(args.lock, args.local_root, document=document, artifacts=artifacts)
            stream = args.local_lock.open("x")
            try:
                with stream:
                    stream.write(output)
            except BaseException:
                args.local_lock.unlink(missing_ok=True)
                raise
    except (OSError, ValueError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
