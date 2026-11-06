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
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from packaging.pylock import PackageArchive, PackageSdist, PackageWheel, Pylock, PylockSelectError, PylockValidationError
from packaging.tags import sys_tags
from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, canonicalize_name, parse_sdist_filename, parse_wheel_filename
from wheelhouse_tool.network import RequestFailure, failure_detail, request, retry


@contextmanager
def pool_lock(pool, exclusive):
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def acquisition_lock(pool):
    # Keep downloads serialized without excluding readers of published files.
    directory = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(".wheelhouse-acquire.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o644, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Cannot acquire wheelhouse lock: expected a regular file.")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            fcntl.flock(directory, fcntl.LOCK_SH)
            yield directory
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def sha256(path, directory_fd=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Cannot use {Path(path).name}: expected a regular file.")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_lock(lock):
    with lock.open("rb") as stream:
        return Pylock.from_dict(tomllib.load(stream))


# silent-defaults: allow-contract -- Optional source-distribution requirements are a fixed empty tuple; absent source-lock inputs retain their independent None sentinel.
def selected(lock, prefer_sdist, require_sdist=(), document=None, source_lock=None):
    if document is None:
        document = read_lock(lock)
    projection = {}
    if document.tool is not None and "nmr-provider-tools" in document.tool:
        projection = document.tool["nmr-provider-tools"]
    source_digest = projection.get("source-lock-sha256")
    source_requirements = None
    if source_lock is not None and source_digest is None:
        raise ValueError("Cannot use projected lock: source lock binding is missing.")
    if source_digest is not None:
        if source_lock is None:
            raise ValueError("Cannot use projected lock: supply the matching source requirements lock.")
        from wheelhouse_tool.locate import read_requirements

        records, _, actual_digest = read_requirements(source_lock)
        if actual_digest != source_digest:
            raise ValueError("Cannot use projected lock: supply the matching source requirements lock.")
        source_requirements = {canonicalize_name(requirement.name): (requirement, hashes) for requirement, hashes in records}
        if len(source_requirements) != len(records):
            raise ValueError("Cannot use projected lock: the source lock repeats a package.")
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
        digest = artifact.hashes.get("sha256")
        if source_requirements is not None:
            entry = source_requirements.get(package.name)
            if entry is None or digest not in entry[1]:
                raise ValueError(f"Cannot use projected lock: {package.name} differs from its source lock.")
            requirement = entry[0]
            if requirement.url is not None and artifact.url != urllib.parse.urldefrag(requirement.url).url:
                raise ValueError(f"Cannot use projected lock: {package.name} has a different locked URL.")
            if requirement.url is None and (package.version is None or not requirement.specifier.contains(package.version, prereleases=True)):
                raise ValueError(f"Cannot use projected lock: {package.name} has a different locked version.")
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
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Cannot acquire {name}: a SHA-256 lock hash is required.")
        if url.scheme not in ("https", "http") or not url.netloc or url.username is not None:
            raise ValueError(f"Cannot acquire {name}: expected an HTTP(S) artifact URL without credentials.")
        if name in seen and seen[name] != digest:
            raise ValueError(f"Cannot acquire {name}: the lock selects conflicting bytes for one filename.")
        seen[name] = digest
        artifacts.append((name, digest, artifact.url, package, artifact))
    if missing := required - found_required:
        raise ValueError(f"Cannot acquire source distribution: {', '.join(sorted(missing))} is not selected by the lock.")
    if source_requirements is not None and {package.name for _, _, _, package, _ in artifacts} != set(source_requirements):
        raise ValueError("Cannot use projected lock: its selected packages differ from the source lock.")
    return artifacts


def inspect_artifact(pool, name, digest, directory_fd=None):
    try:
        actual = sha256(name if directory_fd is not None else pool / name, directory_fd)
    # silent-defaults: allow-exception-outcome -- Missing cached wheel bytes establish a checked negative presence result; hash mismatch remains an error.
    except FileNotFoundError:
        return False
    if actual != digest:
        raise ValueError(f"Cannot use {name}: cached bytes differ from the lock; the existing file is preserved.")
    return True


def download(pool, name, digest, url, directory_fd):
    retry(lambda: download_once(pool, name, digest, url, directory_fd), f"Cannot acquire {name}")


def download_once(pool, name, digest, url, directory_fd):
    temporary = f".wheelhouse-{secrets.token_hex(16)}.part"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    published = False
    winner_verified = False
    winner_conflict = False
    hash_mismatch = False
    try:
        with os.fdopen(descriptor, "wb") as target:
            try:
                source = request(lambda: urllib.request.urlopen(url, timeout=60))
            except ValueError:
                raise ValueError(f"Cannot acquire {name}: the download request is invalid.") from None
            with source:
                checksum = hashlib.sha256()
                length = source.headers.get("Content-Length")
                if isinstance(length, str) and length.isdecimal():
                    length = int(length)
                else:
                    length = None
                received = 0
                while True:
                    block = request(lambda: source.read(1024 * 1024))
                    if not block:
                        break
                    received += len(block)
                    checksum.update(block)
                    target.write(block)
                if length is not None and received < length:
                    raise RequestFailure(http.client.IncompleteRead(b"", length - received)) from None
            if checksum.hexdigest() != digest:
                hash_mismatch = True
                raise ValueError(f"Cannot acquire {name}: downloaded bytes differ from the lock.")
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            published = True
        except FileExistsError:
            try:
                present = inspect_artifact(pool, name, digest, directory_fd)
            except ValueError:
                winner_conflict = True
                raise
            if not present:
                raise FileNotFoundError(f"Cannot acquire {name}: the competing file disappeared.")
            winner_verified = True
        os.fsync(directory_fd)
    finally:
        pending = sys.exception()
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            details = []
            if isinstance(pending, RequestFailure):
                details.append(failure_detail(pending.error))
            elif isinstance(pending, OSError):
                number = ""
                if isinstance(pending.errno, int):
                    number = f" (error number {pending.errno})"
                details.append(f"a local file operation failed{number}")
            if hash_mismatch:
                details.append("downloaded bytes differ from the lock")
            if winner_conflict:
                details.append("the competing final file differs from the lock")
            if published:
                details.append("the verified final file was published")
            elif winner_verified:
                details.append("a competing final file was verified")
            details.append("could not remove the temporary file from the wheelhouse")
            raise OSError(f"Cannot finish acquiring {name}: {'; '.join(details)}.") from None


# silent-defaults: allow-contract -- Explicit absence of source preferences is represented by immutable empty tuples; artifacts=None requests exact lock selection.
def run(action, lock, pool, prefer_sdist=(), require_sdist=(), artifacts=None):
    if artifacts is None:
        artifacts = selected(lock, prefer_sdist, require_sdist)
    if action == "acquire":
        # A complete pool needs no writer lock and may be mounted read-only.
        with pool_lock(pool, exclusive=False) as directory_fd:
            try:
                return _run("check", pool, artifacts, directory_fd)
            # silent-defaults: allow-contract -- A missing cached artifact is the only expected read-only preflight failure; write-lock acquisition follows.
            except FileNotFoundError:
                pass
        with acquisition_lock(pool) as directory_fd:
            return _run(action, pool, artifacts, directory_fd)
    with pool_lock(pool, exclusive=False) as directory_fd:
        return _run(action, pool, artifacts, directory_fd)


def _run(action, pool, artifacts, directory_fd):
    for name, digest, url, _package, _artifact in artifacts:
        if not inspect_artifact(pool, name, digest, directory_fd):
            if action == "check":
                raise FileNotFoundError(f"Cannot use {name}: the artifact is missing from the wheelhouse.")
            download(pool, name, digest, url, directory_fd)
    return {"artifacts": [{"package": str(package.name), "filename": name, "sha256": digest}
                          for name, digest, _, package, _ in artifacts]}


# silent-defaults: allow-contract -- Source distribution preferences default to immutable empty tuples; optional captured document/artifact arguments are absent as None.
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
    parser.add_argument("--source-lock", type=Path, help="Original requirements lock for a metadata projection.")
    parser.add_argument("--prefer-sdist", action="append")
    parser.add_argument("--require-sdist", action="append")
    parser.add_argument("--local-lock", type=Path, help="Write a local-path pylock after a successful operation.")
    parser.add_argument("--local-root", type=Path, help="Absolute pool path inside the offline build.")
    args = parser.parse_args()
    prefer_sdist = []
    require_sdist = []
    if args.prefer_sdist is not None:
        prefer_sdist = args.prefer_sdist
    if args.require_sdist is not None:
        require_sdist = args.require_sdist
    try:
        if args.local_lock is not None and (args.local_root is None or not args.local_root.is_absolute()):
            raise ValueError("Cannot write a local lock: supply an absolute --local-root.")
        if args.local_lock is not None and not (args.local_lock.name == "pylock.toml" or
                                               args.local_lock.name.startswith("pylock.") and args.local_lock.name.endswith(".toml")):
            raise ValueError("Cannot write a local lock: use pylock.toml or pylock.NAME.toml so installers recognize it.")
        document = read_lock(args.lock)
        artifacts = selected(args.lock, prefer_sdist, require_sdist, document, args.source_lock)
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
    # silent-defaults: allow-exception-outcome -- The acquisition CLI reports qualified failures with nonzero exit status, never an empty success.
    except (OSError, ValueError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
