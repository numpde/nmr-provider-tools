"""Capture the issued provider-core wheel and its closed runtime image inputs."""

from tools.container_guard import require_container

require_container()

import argparse
import re
import shutil
import tempfile
from pathlib import Path

from tools.artifacts import regular_file
from tools.wheelhouse import capture, digest, read_json, requirements


def capture_image_inputs(artifacts, pool, destination):
    wheels = list(artifacts.glob("*.whl"))
    if len(wheels) != 1 or re.fullmatch(r"provider_core-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any.whl", wheels[0].name) is None:
        raise ValueError("Cannot capture delivery image inputs: select one issued provider-core release directory.")
    wheel = wheels[0]
    prefix = wheel.name.removesuffix("-py3-none-any.whl")
    manifest = read_json(artifacts / (prefix + "-sha256.json"))
    version = prefix.removeprefix("provider_core-")
    lock = artifacts / (prefix + "-install.lock")
    with tempfile.TemporaryDirectory(prefix="delivery-inputs-") as temporary:
        selected = Path(temporary)
        captured_lock = selected / "provider-core-install.lock"
        with regular_file(lock) as reader, captured_lock.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        runtime_pool = selected / "pool"
        runtime_pool.mkdir()
        capture(Path("/core-runtime.lock"), pool, runtime_pool)
        with regular_file(wheel) as reader, (runtime_pool / wheel.name).open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        if (digest(captured_lock) != manifest[lock.name] or digest(runtime_pool / wheel.name) != manifest[wheel.name]
                or requirements(captured_lock) != requirements(Path("/core-runtime.lock")) | {("provider-core", version): {manifest[wheel.name]}}):
            raise ValueError("Cannot capture delivery image inputs: issued wheel or closed runtime lock differs from its recorded release hashes.")
        target = destination / "wheels"
        target.mkdir()
        capture(captured_lock, runtime_pool, target)
        shutil.copyfile(captured_lock, destination / "provider-core-install.lock")
        (destination / "provider-core-install.lock").chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    capture_image_inputs(args.artifacts, args.pool, args.destination)
