"""Capture declared OS artifacts from an additive shared cache.

Admission and offline installation follow agent-mail tools/dependencies/checker.py
at 5b817df38da3d40857d30e55fdbb99022bb78a70. Per-file publication follows
pay-to-play tools/dependencies/wheel_pool.py at f3df825ef3e92f0873904694435ecdd43b5d87a2.
The shared directory has no receipt or closed inventory; only selected bytes count.
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
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from tools.artifacts import fsync_directory, regular_file, require_download_proxy


def unique_fields(pairs):
    document = {}
    for name, value in pairs:
        if name in document:
            raise ValueError(f"Cannot admit checker dependencies: duplicate JSON field {name!r}.")
        document[name] = value
    return document


def declaration(path):
    with regular_file(path) as stream:
        document = json.load(stream, object_pairs_hook=unique_fields)
    if not isinstance(document, dict) or set(document) != {"platform", "packages"}:
        raise ValueError("Cannot admit checker dependencies: expected platform and packages fields.")
    if document["platform"] != "linux/amd64" or not isinstance(document["packages"], list):
        raise ValueError("Cannot admit checker dependencies: expected Linux/amd64 package list.")
    packages = document["packages"]
    if len(packages) != 1:
        raise ValueError("Cannot admit checker dependencies: this checker requires exactly GNU Make.")
    package = packages[0]
    if not isinstance(package, dict) or set(package) != {"name", "version", "file", "url", "size", "sha256"}:
        raise ValueError("Cannot admit checker dependency: package identity fields differ.")
    if not all(isinstance(package[key], str) for key in ("name", "version", "file", "url", "sha256")):
        raise ValueError("Cannot admit checker dependency: identity fields must be strings.")
    if package["name"] != "make" or re.fullmatch(r"[A-Za-z0-9.+:~_-]+", package["version"]) is None:
        raise ValueError("Cannot admit checker dependency: expected a GNU Make version.")
    filename = f"make_{package['version'].split(':', 1)[-1]}_amd64.deb"
    if package["file"] != filename or package["url"] != f"https://deb.debian.org/debian/pool/main/m/make-dfsg/{filename}":
        raise ValueError("Cannot admit checker dependency: URL must name the declared Debian Make artifact.")
    if not (type(package["size"]) is int and 0 < package["size"] and re.fullmatch(r"[0-9a-f]{64}", package["sha256"]) is not None):
        raise ValueError("Cannot admit checker dependency: expected a positive byte count and SHA-256.")
    return packages


def verify(package, path):
    checksum = hashlib.sha256()
    count = 0
    with regular_file(path) as stream:
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if not count <= package["size"]:
                raise ValueError(f"Cannot admit {path}: exceeds declared size {package['size']}.")
            checksum.update(chunk)
    if count != package["size"] or checksum.hexdigest() != package["sha256"]:
        raise ValueError(f"Cannot admit {path}: size or SHA-256 differs from the dependency declaration.")


def check(packages, pool):
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError(f"Cannot use checker cache {pool}: expected a directory without a symlink.")
    for package in packages:
        verify(package, pool / package["file"])


def capture(packages, pool, destination):
    """Hash copied bytes, so cache replacement cannot rebind build inputs."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Cannot capture checker dependencies: destination must be a private empty directory.")
    check(packages, pool)
    for package in packages:
        copied = destination / package["file"]
        with regular_file(pool / package["file"]) as source, copied.open("xb") as target:
            # Read at most the declared size plus one byte, even if a peer grows the source.
            remaining = package["size"] + 1
            while remaining and (chunk := source.read(min(remaining, 1024 * 1024))):
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
        verify(package, copied)
    fsync_directory(destination)


def publish(package, staged, destination):
    """Publish without replacing an existing cache inode, including a peer's win."""
    verify(package, staged)
    with regular_file(staged) as stream:
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())
    try:
        os.link(staged, destination, follow_symlinks=False)
    except FileExistsError:
        verify(package, destination)
    fsync_directory(destination.parent)


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("Cannot download checker dependency: redirect left HTTPS.")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def download(packages, pool):
    require_download_proxy()
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError(f"Cannot download checker dependencies: shared cache directory is missing: {pool}.")
    for package in packages:
        destination = pool / package["file"]
        if destination.exists() or destination.is_symlink():
            verify(package, destination)
            continue
        print(f"Downloading {package['file']} into the shared wheelhouse.", flush=True)
        opener = urllib.request.build_opener(HttpsRedirects())
        with tempfile.NamedTemporaryFile(prefix=".checker-download-", suffix=".part", dir=pool) as target:
            with opener.open(package["url"], timeout=60) as response:
                remaining = package["size"] + 1
                while remaining and (chunk := response.read(min(remaining, 1024 * 1024))):
                    target.write(chunk)
                    remaining -= len(chunk)
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
            publish(package, Path(target.name), destination)
        fsync_directory(pool)


def install(packages, directory):
    check(packages, directory)
    subprocess.run(["dpkg", "--install", *[str(directory / package["file"]) for package in packages]], check=True)
    for package in packages:
        observed = subprocess.run(
            ["dpkg-query", "--show", "--showformat=${Version}", package["name"]],
            check=True, capture_output=True, text=True,
        ).stdout
        if observed != package["version"]:
            raise ValueError(f"Cannot admit checker image: installed {package['name']} version differs from declaration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "capture", "download", "install"))
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    packages = declaration(args.declaration)
    if args.action == "capture":
        if args.destination is None:
            parser.error("capture requires --destination")
        capture(packages, args.directory, args.destination)
    elif args.action == "download":
        download(packages, args.directory)
    elif args.action == "install":
        install(packages, args.directory)
    else:
        check(packages, args.directory)


if __name__ == "__main__":
    main()
