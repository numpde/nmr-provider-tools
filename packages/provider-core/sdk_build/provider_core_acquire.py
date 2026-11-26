"""Acquire complete SDK environment locks with independently pinned Wheelhouse."""

from tools.container_guard import require_container

require_container()

import json
import pathlib
import subprocess
import sys
import tempfile
import urllib.request

from tools.artifacts import require_download_proxy
from tools.wheelhouse import digest, pool_lock, publish_file


def tool_present(pool, pin):
    tool_path = pool / pin["name"]

    if (not tool_path.exists()) and (not tool_path.is_symlink()):
        return False

    tool_sha256 = digest(tool_path)
    expected_sha256 = pin["sha256"]

    if not (tool_sha256 == expected_sha256):
        raise ValueError(f"{tool_path=}, {tool_sha256=} ≠ {expected_sha256=}")

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

            if not (download_sha256 == expected_sha256):
                raise ValueError(f"{tool_path=}, {download_sha256=} ≠ {expected_sha256=}")

            publish_file(pathlib.Path(temporary.name), tool_path, expected_sha256)

    return tool_path


def wheelhouse_command(tool, action, lock, pool):
    return [
        sys.executable,  # Match the interpreter that owns the SDK target locks.
        "-I",  # Ignore host Python paths and environment configuration.
        "-B",  # Avoid bytecode writes from the independent bundle.
        str(tool),  # Execute the authenticated external Wheelhouse artifact.
        action,  # Acquire or verify the already resolved selection.
        "--lock",  # Supply one complete environment lock.
        str(lock),  # Read the SDK-owned target selection.
        "--pool",  # Select the shared artifact cache.
        str(pool),  # Keep the cache location explicit for every invocation.
    ]


def check_environments(pool, locks, pin):
    """Fail offline when the independent tool or any selected wheel is unavailable."""
    tool_path = pool / pin["name"]

    if not (tool_present(pool, pin)):
        raise FileNotFoundError(
            f"Cannot verify SDK environments: pinned Wheelhouse is missing at {tool_path=}. "
            f"Run make packages/provider-core/download for this wheelhouse."
        )

    tool = pool / pin["name"]

    for lock in locks:
        subprocess.run(wheelhouse_command(tool, "check", lock, pool), check=True, stdout=subprocess.DEVNULL)


def acquire_environments(pool, locks, pin):
    """Follow already generated selections; runtime acquisition never resolves."""
    tool = acquire_tool(pool, pin)

    for lock in locks:
        subprocess.run(wheelhouse_command(tool, "acquire", lock, pool), check=True)


def main():
    arguments = sys.argv[1:]

    if not (arguments in ([], ["check"], ["verify"], ["tool"])):
        raise ValueError(f"Supply check, verify, tool, or no argument to acquire SDK environments; received {arguments=}.")

    pool = pathlib.Path("/wheelhouse")

    locks = (
        pathlib.Path("/core-runtime.wheelhouse.json"),
        pathlib.Path("/core-build.wheelhouse.json"),
        pathlib.Path("/core-test.wheelhouse.json"),
        pathlib.Path("/acquisition-bootstrap.wheelhouse.json"),
    )

    pin = json.loads(pathlib.Path("/wheelhouse-tool-release.json").read_text())

    if arguments in (["check"], ["verify"]):
        check_environments(pool, locks, pin)

        print("complete")
    elif arguments == ["tool"]:
        acquire_tool(pool, pin)
    else:
        acquire_environments(pool, locks, pin)


if __name__ == "__main__":
    main()
