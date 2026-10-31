"""Acquire and capture the pinned shared-delivery release in the common wheelhouse."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from tools.artifacts import regular_file, require_download_proxy
from tools.wheelhouse import capture, digest, publish_file, read_json, requirements


def release_inputs(pin, runtime_lock):
    release = read_json(pin)
    version, wheel_hash = release["version"], release["wheel_sha256"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9a-f]{64}", wheel_hash):
        raise ValueError("Cannot use delivery release: invalid pinned version or wheel hash.")
    with regular_file(runtime_lock) as source:
        runtime = source.read().decode("utf-8")
    lock = (runtime.rstrip() + f"\nprovider-core=={version} \\\n    --hash=sha256:{wheel_hash}\n").encode()
    if hashlib.sha256(lock).hexdigest() != release["install_lock_sha256"]:
        raise ValueError("Cannot use delivery release: runtime lock differs from the issued install lock.")
    return release, lock


def release_wheels(release):
    wheel = f"provider_core-{release['version']}-py3-none-any.whl"
    return dict(release["runtime_wheels"]) | {wheel: release["wheel_sha256"]}


def missing_wheels(release, pool):
    if not pool.is_dir() or pool.is_symlink():
        raise ValueError("Cannot use delivery release: wheelhouse must be an existing directory.")
    missing = []
    for name, expected in release_wheels(release).items():
        if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", name) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Cannot use delivery release: malformed pinned wheel identity.")
        target = pool / name
        if target.exists() or target.is_symlink():
            if digest(target) != expected:
                raise ValueError(f"Cannot reuse {name}: cached bytes differ from the pinned release; existing file retained.")
        else:
            missing.append((name, expected))
    return missing


def acquire_release(pin, runtime_lock, pool):
    release, _ = release_inputs(pin, runtime_lock)
    for name, expected in missing_wheels(release, pool):
        require_download_proxy()
        if name.startswith("provider_core-"):
            url = f"https://github.com/numpde/nmr-provider-tools/releases/download/v{release['version']}/{name}"
        else:
            distribution, version, *_ = name.split("-")
            with urllib.request.urlopen(f"https://pypi.org/pypi/{distribution}/{version}/json", timeout=30) as response:
                metadata = json.load(response)
            candidates = [item["url"] for item in metadata["urls"]
                          if item["filename"] == name and item["digests"]["sha256"] == expected]
            if len(candidates) != 1 or urllib.parse.urlsplit(candidates[0]).hostname != "files.pythonhosted.org":
                raise ValueError(f"Cannot acquire {name}: the pinned wheel is absent from its published index.")
            url = candidates[0]
        with tempfile.NamedTemporaryFile(prefix=".delivery-download-", suffix=".part", dir=pool) as destination:
            with urllib.request.urlopen(url, timeout=60) as source:
                shutil.copyfileobj(source, destination)
            destination.flush()
            if digest(Path(destination.name)) != expected:
                raise ValueError(f"Cannot acquire {name}: downloaded bytes do not match the pinned release.")
            publish_file(Path(destination.name), pool / name, expected)


def capture_image_inputs(pin, runtime_lock, pool, destination):
    release, lock_bytes = release_inputs(pin, runtime_lock)
    with tempfile.TemporaryDirectory(prefix="delivery-inputs-") as temporary:
        lock = Path(temporary) / "provider-core-install.lock"
        lock.write_bytes(lock_bytes)
        requirements(lock)
        target = destination / "wheels"
        target.mkdir()
        pinned_pool = Path(temporary) / "pool"
        pinned_pool.mkdir()
        for name, expected in release_wheels(release).items():
            if digest(pool / name) != expected:
                raise ValueError(f"Cannot build delivery: {name} differs from the pinned release.")
            shutil.copyfile(pool / name, pinned_pool / name)
        selected = capture(lock, pinned_pool, target)
        if selected["files"] != release_wheels(release):
            raise ValueError("Cannot build delivery: selected wheels differ from the pinned release closure.")
        shutil.copyfile(lock, destination / "provider-core-install.lock")
        (destination / "provider-core-install.lock").chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "acquire", "capture"))
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.action == "check":
        release, _ = release_inputs(args.pin, args.runtime_lock)
        if missing_wheels(release, args.pool):
            print("missing")
        else:
            print("complete")
    elif args.action == "acquire":
        acquire_release(args.pin, args.runtime_lock, args.pool)
    else:
        if args.destination is None:
            parser.error("capture requires --destination")
        capture_image_inputs(args.pin, args.runtime_lock, args.pool, args.destination)
