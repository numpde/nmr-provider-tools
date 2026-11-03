"""Acquire provider-core build dependencies into the shared pool."""

from tools.container_guard import require_container

require_container()

import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from tools.artifacts import require_download_proxy
from tools.wheelhouse import digest, pool_lock, publish_file, requirements, select_wheels


TOOL_NAME = "wheelhouse-tool-0.5.9.pyz"
TOOL_SHA256 = "b504df2889d99ae122173a06e192b7e4310ea66e42a150059310a00a573abbe7"
TOOL_URL = "https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.9/" + TOOL_NAME


def cache_state(pool, locks):
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from pip._vendor.packaging.version import Version

    target_tags = set(sys_tags())
    expected_locks = [(lock, {(name, Version(version)): hashes for (name, version), hashes in requirements(lock).items()})
                      for lock in locks]
    missing = False
    with pool_lock(pool) as lock_fd:
        for lock, expected in expected_locks:
            found = set()
            conflicting = {}
            for path in pool.glob("*.whl"):
                try:
                    name, version, _, tags = parse_wheel_filename(path.name)
                except InvalidWheelFilename:
                    continue
                identity = (name, version)
                if identity not in expected or not tags.intersection(target_tags):
                    continue
                if digest(path) not in expected[identity]:
                    conflicting[identity] = path.name
                else:
                    found.add(identity)
            if unavailable := set(conflicting) - found:
                name = conflicting[next(iter(unavailable))]
                raise ValueError(f"Cannot reuse {name}: cached bytes differ from the lock.")
            if found != set(expected):
                missing = True
            else:
                select_wheels(lock, pool, lock_fd=lock_fd)
    return "missing" if missing else "complete"


def acquire_tool(pool):
    path = pool / TOOL_NAME
    with pool_lock(pool, exclusive=True):
        if path.exists() or path.is_symlink():
            if digest(path) != TOOL_SHA256:
                raise ValueError("Cannot reuse the wheelhouse tool: its bytes differ from the pinned release.")
            return path
        require_download_proxy()
        with tempfile.NamedTemporaryFile(prefix=".wheelhouse-tool-", suffix=".part", dir=pool) as temporary:
            with urllib.request.urlopen(TOOL_URL, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
            temporary.flush()
            if digest(Path(temporary.name)) != TOOL_SHA256:
                raise ValueError("Cannot acquire the wheelhouse tool: its bytes differ from the pinned release.")
            publish_file(Path(temporary.name), path, TOOL_SHA256)
    return path


def main():
    pool = Path("/wheelhouse")
    locks = (Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse-tool.lock"))
    if sys.argv[1:] == ["check"]:
        print(cache_state(pool, locks))
        return
    tool = acquire_tool(pool)
    with tempfile.TemporaryDirectory(prefix="provider-core-locks-") as temporary:
        for number, lock in enumerate(locks):
            projection = Path(temporary) / f"pylock.{number}.toml"
            command = [sys.executable, "-I", "-B", str(tool)]
            with projection.open("xb") as output:
                subprocess.run(command + ["locate", "--lock", str(lock), "--index-url", "https://pypi.org/simple",
                                          "--pool", str(pool)], stdout=output, check=True)
            subprocess.run(command + ["acquire", "--lock", str(projection), "--source-lock", str(lock),
                                      "--pool", str(pool)], check=True)


if __name__ == "__main__":
    main()
