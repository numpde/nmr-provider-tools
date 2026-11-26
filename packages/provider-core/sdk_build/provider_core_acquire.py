"""Acquire hashed SDK requirements and exact cold-bootstrap wheels."""

from tools.container_guard import require_container

require_container()

import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse

from tools.adapters.http_download import https_download_opener
from tools.adapters.pip_install import pip_command
from tools.artifacts import require_download_proxy
from tools.wheelhouse import digest, pool_lock, publish_file, select_wheels


def bootstrap_wheels(lock):
    """Read the exact wheel sources declared by the cold bootstrap."""
    bootstrap_lock = tomllib.loads(lock.read_text())

    for package in bootstrap_lock["packages"]:
        (wheel,) = package["wheels"]

        yield {
            "name": pathlib.PurePosixPath(urllib.parse.urlsplit(wheel["url"]).path).name,
            "url": wheel["url"],
            "sha256": wheel["hashes"]["sha256"],
        }


def verify_artifact(pool, artifact):
    artifact_path = pool / artifact["name"]
    observed_sha256 = digest(artifact_path)
    expected_sha256 = artifact["sha256"]

    if not (observed_sha256 == expected_sha256):
        raise ValueError(
            f"Cannot use cached dependency {artifact_path = !s}: {observed_sha256 = } differs from "
            f"{expected_sha256 = }. The existing file was retained."
        )

    return artifact_path


def acquire_artifact(pool, artifact):
    """Publish exact HTTPS bytes without replacing an existing cache file."""
    artifact_path = pool / artifact["name"]

    with pool_lock(pool):
        if (artifact_path.exists()) or (artifact_path.is_symlink()):
            return verify_artifact(pool, artifact)

    with pool_lock(pool, exclusive=True):
        if (artifact_path.exists()) or (artifact_path.is_symlink()):
            return verify_artifact(pool, artifact)

        require_download_proxy()

        with tempfile.NamedTemporaryFile(prefix=".dependency-", suffix=".part", dir=pool) as temporary:
            with https_download_opener().open(artifact["url"], timeout=60) as response:
                shutil.copyfileobj(response, temporary, 1024 * 1024)

            temporary.flush()
            staged = pathlib.Path(temporary.name)
            observed_sha256 = digest(staged)
            expected_sha256 = artifact["sha256"]

            if not (observed_sha256 == expected_sha256):
                raise ValueError(
                    f"Cannot acquire dependency {artifact_path = !s}: {observed_sha256 = } differs from "
                    f"{expected_sha256 = }. No cache file was published."
                )

            publish_file(staged, artifact_path, expected_sha256)

    return artifact_path


def acquire_requirements(pool, lock):
    """Let pip download the pinned closure, then publish its verified wheels."""
    require_download_proxy()

    with pool_lock(pool, exclusive=True) as descriptor:
        with tempfile.TemporaryDirectory(prefix=".dependency-download-", dir=pool) as temporary:
            destination = pathlib.Path(temporary)

            subprocess.run(
                [
                    *pip_command(sys.executable),
                    "download",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--require-hashes",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--find-links",
                    str(pool),
                    "--dest",
                    str(destination),
                    "--requirement",
                    str(lock),
                ],
                check=True,
                pass_fds=(descriptor,),
            )

            selected = select_wheels(lock, destination)

            for (name, checksum) in selected["files"].items():
                publish_file(destination / name, pool / name, checksum)


def check_environments(pool, locks, bootstrap):
    with pool_lock(pool) as descriptor:
        for lock in locks:
            select_wheels(lock, pool, lock_fd=descriptor)

        for artifact in bootstrap_wheels(bootstrap):
            verify_artifact(pool, artifact)


def acquire_environments(pool, locks, bootstrap):
    for lock in locks:
        acquire_requirements(pool, lock)

    for artifact in bootstrap_wheels(bootstrap):
        acquire_artifact(pool, artifact)


def main():
    arguments = sys.argv[1:]

    if not (arguments in ([], ["check"], ["verify"])):
        raise ValueError(f"Supply check, verify, or no argument to acquire SDK dependencies; received {arguments = }.")

    pool = pathlib.Path("/wheelhouse")
    locks = [pathlib.Path("/core-" + role + ".lock") for role in ("runtime", "build", "test")]
    bootstrap = pathlib.Path("/acquisition-bootstrap.pylock.toml")

    if arguments:
        check_environments(pool, locks, bootstrap)
        print("complete")
    else:
        acquire_environments(pool, locks, bootstrap)


if __name__ == "__main__":
    main()
