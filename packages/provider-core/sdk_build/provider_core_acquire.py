"""Acquire provider-core build dependencies into the shared pool."""

from tools.container_guard import require_container

require_container()

import json
import pathlib
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request

from tools.artifacts import require_download_proxy
from tools.adapters import pylock
from tools.wheelhouse import digest, pool_lock, publish_file, requirements, select_wheels


def cache_state(pool, locks):
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.specifiers import Specifier
    from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from pip._vendor.packaging.version import Version

    target_tags = set(sys_tags())

    expected_locks = [
        (lock, {(name, Version(version)): hashes for ((name, version), hashes) in requirements(lock).items()})
        for lock in locks
    ]

    missing = False

    with pool_lock(pool) as lock_fd:
        for (lock, expected) in expected_locks:
            specifiers = {identity: Specifier(f"=={identity[1]}") for identity in expected}
            found = set()
            conflicting = {}

            for wheel_path in pool.glob("*.whl"):
                try:
                    (name, version, _, tags) = parse_wheel_filename(wheel_path.name)
                except InvalidWheelFilename:
                    continue

                if not tags.intersection(target_tags):
                    continue

                for (identity, specifier) in specifiers.items():
                    if (identity[0] != name) or (not specifier.contains(version, prereleases=True)):
                        continue

                    wheel_sha256 = digest(wheel_path)

                    if wheel_sha256 not in expected[identity]:
                        conflicting[identity] = (wheel_path, wheel_sha256)
                    else:
                        found.add(identity)

            if unavailable := set(conflicting) - found:
                identity = next(iter(unavailable))
                (wheel_path, wheel_sha256) = conflicting[identity]
                expected_sha256s = sorted(expected[identity])

                raise ValueError(f"{wheel_path=}, {wheel_sha256=} ∉ {expected_sha256s=}")

            if found != set(expected):
                missing = True
            else:
                select_wheels(lock, pool, lock_fd=lock_fd)

    if missing:
        return "missing"

    return "complete"


def tool_present(pool, pin):
    tool_path = pool / pin["name"]

    if (not tool_path.exists()) and (not tool_path.is_symlink()):
        return False

    tool_sha256 = digest(tool_path)
    expected_sha256 = pin["sha256"]

    if tool_sha256 != expected_sha256:
        raise ValueError(f"{tool_path=}, {tool_sha256=} ≠ {expected_sha256=}")

    return True


def bootstrap_present(pool, lock):
    (package,) = pylock.packages(tomllib.loads(lock.read_text()))
    (wheel,) = pylock.wheels(package)
    wheel_path = pool / pathlib.Path(urllib.parse.urlsplit(pylock.artifact_url(wheel)).path).name

    if (not wheel_path.exists()) and (not wheel_path.is_symlink()):
        return False

    wheel_sha256 = digest(wheel_path)
    bootstrap_sha256 = pylock.artifact_sha256(wheel)

    if wheel_sha256 != bootstrap_sha256:
        raise ValueError(f"{wheel_path=}, {wheel_sha256=} ≠ {bootstrap_sha256=}")

    return True


def acquire_tool(pool, pin):
    tool_path = pool / pin["name"]

    with pool_lock(pool, exclusive=False):
        if tool_present(pool, pin):
            return tool_path

    with pool_lock(pool, exclusive=True):
        if tool_present(pool, pin):
            return tool_path

        require_download_proxy()

        with tempfile.NamedTemporaryFile(prefix=".wheelhouse-tool-", suffix=".part", dir=pool) as temporary:
            with urllib.request.urlopen(pin["url"], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)

            temporary.flush()

            download_sha256 = digest(pathlib.Path(temporary.name))
            expected_sha256 = pin["sha256"]

            if download_sha256 != expected_sha256:
                raise ValueError(f"{tool_path=}, {download_sha256=} ≠ {expected_sha256=}")

            publish_file(pathlib.Path(temporary.name), tool_path, expected_sha256)

    return tool_path


def main():
    pool = pathlib.Path("/wheelhouse")
    locks = (pathlib.Path("/core-runtime.lock"), pathlib.Path("/core-build.lock"), pathlib.Path("/core-test.lock"), pathlib.Path("/wheelhouse-tool.lock"))
    bootstrap = pathlib.Path("/acquisition-bootstrap.pylock.toml")

    if sys.argv[1:] in (["check"], ["verify"]):
        state = cache_state(pool, locks)

        if not bootstrap_present(pool, bootstrap):
            state = "missing"

        if (sys.argv[1:] == ["verify"]) and (state != "complete"):
            raise ValueError(
                "Provider-core build dependencies are missing. "
                "Run make packages/provider-core/download for this wheelhouse."
            )

        print(state)

        return

    pin = json.loads(pathlib.Path("/wheelhouse-tool-release.json").read_text())
    tool = acquire_tool(pool, pin)

    if sys.argv[1:] == ["tool"]:
        return

    with tempfile.TemporaryDirectory(prefix="provider-core-locks-") as temporary:
        for (number, lock) in enumerate(locks):
            projection = pathlib.Path(temporary) / f"pylock.{number}.toml"

            command = [
                sys.executable,  # Use the worker's Python interpreter
                "-I",  # Ignore PYTHON* settings and the user site
                "-B",  # Suppress bytecode writes
                str(tool),  # Run the pinned wheelhouse zipapp
            ]

            with projection.open("xb") as output:
                subprocess.run(
                    command + [
                        "locate",  # Resolve artifact URLs for the pinned hashes
                        "--lock",  # Read the source requirements lock
                        str(lock),  # Use the current lock in the acquisition batch
                        "--index-url",  # Select the package index explicitly
                        "https://pypi.org/simple",  # Use the public PyPI simple index
                        "--pool",  # Consult the shared artifact cache
                        str(pool),  # Look for existing artifacts in this directory
                    ],
                    stdout=output,  # Keep the projected lock for acquisition
                    check=True,  # Stop when source lookup fails
                )

            subprocess.run(
                command + [
                    "acquire",  # Fetch missing artifacts and verify cached copies
                    "--lock",  # Select artifacts from the projected lock
                    str(projection),  # Supply the URLs resolved by locate
                    "--source-lock",  # Verify the projection against its source lock
                    str(lock),  # Supply the original requirements and hashes
                    "--pool",  # Select the shared artifact cache
                    str(pool),  # Publish complete artifacts in this directory
                ],
                check=True,  # Stop if the selected artifacts cannot be admitted
            )

    subprocess.run(
        [
            sys.executable,  # Use the worker's Python interpreter
            "-I",  # Ignore PYTHON* settings and the user site
            "-B",  # Suppress bytecode writes
            str(tool),  # Run the pinned wheelhouse zipapp
            "acquire",  # Fetch missing artifacts and verify cached copies
            "--lock",  # Select artifacts from an explicit lock
            str(bootstrap),  # Use the separately pinned acquisition dependency
            "--pool",  # Select the shared artifact cache
            str(pool),  # Reuse or publish artifacts in this directory
        ],
        check=True,  # Propagate bootstrap dependency acquisition failure
    )


if __name__ == "__main__":
    main()
