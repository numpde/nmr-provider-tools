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
import reprlib
import shutil
import tempfile
import pathlib

from tools.adapters import debian
from tools.adapters.http_download import https_download_opener
from tools.artifacts import fsync_directory, regular_file, require_download_proxy
from tools.installer import duplicate_json_fields
from tools.installer import install as install_pip, release as installer_release
from tools.validation.upstream.debian import require_binary_package_filename
from type_requirements import require_instance, require_type


def unique_fields(field_pairs):
    if extra_field_occurrences := duplicate_json_fields(field_pairs):
        raise ValueError(f"Checker dependency fields should be unique, but {extra_field_occurrences=}")

    return dict(field_pairs)


def declaration(path):
    with regular_file(path) as stream:
        document = json.load(stream, object_pairs_hook=unique_fields)

    require_instance(document, dict)

    if set(document) != { "platform", "packages" }:
        raise ValueError("Cannot admit checker dependencies: expected platform and packages fields.")

    if document["platform"] != "linux/amd64":
        raise ValueError("Cannot admit checker dependencies: expected Linux/amd64 package list.")

    packages = document["packages"]

    require_instance(packages, list)

    if len(packages) != 1:
        raise ValueError("Cannot admit checker dependencies: this checker requires exactly GNU Make.")

    package = packages[0]

    require_instance(package, dict)

    if set(package) != { "name", "version", "file", "url", "size", "sha256" }:
        raise ValueError("Cannot admit checker dependency: package identity fields differ.")

    require_instance(package["name"], str)
    require_instance(package["version"], str)
    require_instance(package["file"], str)
    require_instance(package["url"], str)
    require_instance(package["sha256"], str)

    package_name = package["name"]
    package_version = package["version"]
    filename = package["file"]
    expected_package_name = "make"

    if not (package_name == expected_package_name):
        raise ValueError(f"{reprlib.repr(package_name)=} should match {expected_package_name=}")

    version_pattern = r"[A-Za-z0-9.+:~_-]+"

    if not re.fullmatch(version_pattern, package_version):
        raise ValueError(f"{reprlib.repr(package_version)=} should match {version_pattern=}")

    require_binary_package_filename(filename, package_name=package_name, package_version=package_version, architecture="amd64")
    download_url = package["url"]
    expected_download_url = f"https://deb.debian.org/debian/pool/main/m/make-dfsg/{filename}"  # machine-string: selected Debian archive URL

    if not (download_url == expected_download_url):
        raise ValueError(f"{reprlib.repr(download_url)=} should match {reprlib.repr(expected_download_url)=}")

    require_type(package["size"], int)

    if not ((0 < package["size"]) and (re.fullmatch(r"[0-9a-f]{64}", package["sha256"]) is not None)):
        raise ValueError("Cannot admit checker dependency: expected a positive byte count and SHA-256.")

    return packages


def verify(package, dependency_path):
    checksum = hashlib.sha256()
    byte_count = 0

    with regular_file(dependency_path) as stream:
        while chunk := stream.read(1024 * 1024):
            byte_count += len(chunk)

            if not (byte_count <= package["size"]):
                raise ValueError(f"{dependency_path=}: {byte_count=} should not exceed {package['size']=}")

            checksum.update(chunk)

    if not (byte_count == package["size"]):
        raise ValueError(f"{dependency_path=}: {byte_count=} should match {package['size']=}")

    file_sha256 = checksum.hexdigest()

    if not (file_sha256 == package["sha256"]):
        raise ValueError(f"{dependency_path=}: {file_sha256=} should match {package['sha256']=}")


def check(packages, pool):
    if (pool.is_symlink()) or (not pool.is_dir()):
        raise ValueError(f"{pool=} should be a directory without a symlink")

    for package in packages:
        verify(package, pool / package["file"])


def capture(packages, pool, destination):
    """Hash copied bytes, so cache replacement cannot rebind build inputs."""

    if (destination.is_symlink()) or (not destination.is_dir()) or (any(destination.iterdir())):
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


def download(packages, pool):
    require_download_proxy()

    if (pool.is_symlink()) or (not pool.is_dir()):
        raise ValueError(f"{pool=} should be a directory without a symlink")

    for package in packages:
        destination = pool / package["file"]

        if destination.exists() or destination.is_symlink():
            verify(package, destination)
            continue

        print(f"Downloading {package['file']=} into {pool=}", flush=True)
        opener = https_download_opener()

        with tempfile.NamedTemporaryFile(prefix=".checker-download-", suffix=".part", dir=pool) as target:
            with opener.open(package["url"], timeout=60) as response:
                remaining = package["size"] + 1

                while remaining and (chunk := response.read(min(remaining, 1024 * 1024))):
                    target.write(chunk)
                    remaining -= len(chunk)

            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
            publish(package, pathlib.Path(target.name), destination)

        fsync_directory(pool)


def install(packages, directory):
    check(packages, directory)
    debian.install_binary_packages([(directory / package["file"]) for package in packages])

    for package in packages:
        package_name = package["name"]

        installed_version = debian.installed_package_version(package_name)
        expected_version = package["version"]

        if not (installed_version == expected_version):
            raise ValueError(
                f"After installing {package_name=}, "
                f"{reprlib.repr(installed_version)=} should match {reprlib.repr(expected_version)=}"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("check", "capture", "download", "install"))
    parser.add_argument("--declaration", type=pathlib.Path, required=True)
    parser.add_argument("--directory", type=pathlib.Path, required=True)
    parser.add_argument("--destination", type=pathlib.Path)
    parser.add_argument("--installer", type=pathlib.Path)
    args = parser.parse_args()
    packages = declaration(args.declaration)
    artifacts = list(packages)

    if args.installer is not None:
        record = installer_release(args.installer)

        artifacts.append(record | { "file": record["name"] })

    if args.action == "capture":
        if args.destination is None:
            parser.error("capture requires --destination")

        capture(artifacts, args.directory, args.destination)
    elif args.action == "download":
        download(artifacts, args.directory)
    elif args.action == "install":
        install(packages, args.directory)

        if args.installer is not None:
            install_pip(args.directory, record)
    else:
        check(artifacts, args.directory)


if __name__ == "__main__":
    main()
