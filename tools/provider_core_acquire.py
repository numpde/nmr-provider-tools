"""Acquire provider-core build dependencies into the shared pool."""

from tools.container_guard import require_container

require_container()

import json
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from tools.artifacts import require_download_proxy
from tools.wheelhouse import digest, pool_lock, publish_file, requirements, select_wheels


def cache_state(pool, locks):
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.specifiers import Specifier
    from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from pip._vendor.packaging.version import Version

    target_tags = set(sys_tags())
    expected_locks = [(lock, {(name, Version(version)): hashes for (name, version), hashes in requirements(lock).items()})
                      for lock in locks]
    missing = False
    with pool_lock(pool) as lock_fd:
        for lock, expected in expected_locks:
            specifiers = {identity: Specifier(f"=={identity[1]}") for identity in expected}
            found = set()
            conflicting = {}
            for path in pool.glob("*.whl"):
                try:
                    name, version, _, tags = parse_wheel_filename(path.name)
                except InvalidWheelFilename:
                    continue
                if not tags.intersection(target_tags):
                    continue
                for identity, specifier in specifiers.items():
                    if identity[0] != name or not specifier.contains(version, prereleases=True):
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
    if missing:
        return "missing"
    return "complete"


def tool_present(pool, pin):
    path = pool / pin["name"]
    if not path.exists() and not path.is_symlink():
        return False
    if digest(path) != pin["sha256"]:
        raise ValueError("Cannot reuse the wheelhouse tool: its bytes differ from the pinned release.")
    return True


def bootstrap_present(pool, lock):
    package, = tomllib.loads(lock.read_text())["packages"]
    wheel, = package["wheels"]
    path = pool / Path(urllib.parse.urlsplit(wheel["url"]).path).name
    if not path.exists() and not path.is_symlink():
        return False
    if digest(path) != wheel["hashes"]["sha256"]:
        raise ValueError("Cannot reuse the acquisition identity wheel: its bytes differ from the bootstrap pin.")
    return True


def acquire_tool(pool, pin):
    path = pool / pin["name"]
    with pool_lock(pool, exclusive=False):
        if tool_present(pool, pin):
            return path
    with pool_lock(pool, exclusive=True):
        if tool_present(pool, pin):
            return path
        require_download_proxy()
        with tempfile.NamedTemporaryFile(prefix=".wheelhouse-tool-", suffix=".part", dir=pool) as temporary:
            with urllib.request.urlopen(pin["url"], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
            temporary.flush()
            if digest(Path(temporary.name)) != pin["sha256"]:
                raise ValueError("Cannot acquire the wheelhouse tool: its bytes differ from the pinned release.")
            publish_file(Path(temporary.name), path, pin["sha256"])
    return path


def main():
    pool = Path("/wheelhouse")
    locks = (Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse-tool.lock"))
    bootstrap = Path("/acquisition-bootstrap.pylock.toml")
    if sys.argv[1:] in (["check"], ["verify"]):
        state = cache_state(pool, locks)
        if not bootstrap_present(pool, bootstrap):
            state = "missing"
        if sys.argv[1:] == ["verify"] and state != "complete":
            raise ValueError("Cannot use provider-core build inputs: acquire the missing locked dependencies first.")
        print(state)
        return
    pin = json.loads(Path("/wheelhouse-tool-release.json").read_text())
    tool = acquire_tool(pool, pin)
    if sys.argv[1:] == ["tool"]:
        return
    with tempfile.TemporaryDirectory(prefix="provider-core-locks-") as temporary:
        for number, lock in enumerate(locks):
            projection = Path(temporary) / f"pylock.{number}.toml"
            command = [sys.executable, "-I", "-B", str(tool)]
            with projection.open("xb") as output:
                subprocess.run(command + ["locate", "--lock", str(lock), "--index-url", "https://pypi.org/simple",
                                          "--pool", str(pool)], stdout=output, check=True)
            subprocess.run(command + ["acquire", "--lock", str(projection), "--source-lock", str(lock),
                                      "--pool", str(pool)], check=True)
    subprocess.run([sys.executable, "-I", "-B", str(tool), "acquire", "--lock", str(bootstrap),
                    "--pool", str(pool)], check=True)


if __name__ == "__main__":
    main()
