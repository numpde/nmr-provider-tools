"""Capture the pinned delivery release using pip's hashed wheel selection."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import pathlib
import re
import reprlib
import tempfile

from tools.adapters.github_release import release_asset_url
from tools.adapters.pip_install import hashed_requirement
from tools.artifacts import regular_file
from tools.installer import release as installer_release
from sdk_build.provider_core_acquire import acquire_artifact, acquire_requirements
from tools.wheelhouse import pool_lock, read_json, select_wheels


def release_inputs(pin, runtime_lock):
    release = read_json(pin)
    (release_version, wheel_digest) = (release["version"], release["wheel_sha256"])

    expected_version_pattern = r"[0-9]+\.[0-9]+\.[0-9]+"

    if not re.fullmatch(expected_version_pattern, release_version):
        raise ValueError(f"{reprlib.repr(release_version)=} should match {expected_version_pattern=}")

    expected_digest_pattern = r"[0-9a-f]{64}"

    if not re.fullmatch(expected_digest_pattern, wheel_digest):
        raise ValueError(f"{reprlib.repr(wheel_digest)=} should match {expected_digest_pattern=}")

    with regular_file(runtime_lock) as source:
        runtime_requirements = source.read().decode("utf-8")

    install_lock_bytes = (
        runtime_requirements.rstrip()
        + "\n"
        + hashed_requirement("provider-core", release_version, wheel_digest)
    ).encode()

    install_lock_sha256 = hashlib.sha256(install_lock_bytes).hexdigest()
    expected_install_lock_sha256 = release["install_lock_sha256"]

    if not (install_lock_sha256 == expected_install_lock_sha256):
        raise ValueError(f"{install_lock_sha256=} should match {expected_install_lock_sha256=}")

    return (release, install_lock_bytes)


def sdk_artifact(release):
    name = "provider_core-" + release["version"] + "-py3-none-any.whl"

    return {
        "name": name,
        "sha256": release["wheel_sha256"],
        "url": release_asset_url("numpde/nmr-provider-tools", "v" + release["version"], name),
    }


def check_release(pin, runtime_lock, pool):
    (release, lock_bytes) = release_inputs(pin, runtime_lock)

    with tempfile.TemporaryDirectory(prefix="delivery-check-") as temporary:
        lock = pathlib.Path(temporary) / "install.lock"

        lock.write_bytes(lock_bytes)

        with pool_lock(pool) as descriptor:
            selected = select_wheels(lock, pool, lock_fd=descriptor)

    expected = dict(release["runtime_wheels"]) | {sdk_artifact(release)["name"]: release["wheel_sha256"]}

    if not (selected["files"] == expected):
        raise ValueError(f"Cannot use delivery release {pin = !s}: selected wheels differ from its recorded artifact inventory.")

    return selected


def acquire_release(pin, runtime_lock, pool):
    (release, lock_bytes) = release_inputs(pin, runtime_lock)

    acquire_artifact(pool, sdk_artifact(release))

    with tempfile.TemporaryDirectory(prefix="delivery-acquire-") as temporary:
        lock = pathlib.Path(temporary) / "install.lock"

        lock.write_bytes(lock_bytes)
        acquire_requirements(pool, lock)

    check_release(pin, runtime_lock, pool)


def capture_image_inputs(pin, runtime_lock, pool, destination):
    captured_pin = destination / "delivery-release.json"
    captured_runtime = destination / "delivery-runtime.lock"

    captured_pin.write_bytes(pin.read_bytes())
    captured_runtime.write_bytes(runtime_lock.read_bytes())
    selected = check_release(captured_pin, captured_runtime, pool)

    installation = "".join(
        ((pathlib.Path("/wheels") / name).as_uri() + " --hash=sha256:" + checksum + "\n")
        for (name, checksum) in sorted(selected["files"].items())
    )

    (destination / "provider-core-install.lock").write_text(installation)
    template = destination / "operations/delivery/image/Dockerfile"
    marker = "# selected-wheel-inputs"
    contents = template.read_text()

    if not (contents.count(marker) == 1):
        raise ValueError(f"Cannot capture delivery Dockerfile: {template = !s} must contain exactly one {marker = !r} marker.")

    installer = installer_release(destination / "requirements/installer-release.json")
    artifacts = []
    copies = []

    for (name, checksum) in sorted(selected["files"].items()):
        relative = "wheelhouse/" + name

        artifacts.append({"path": str(pool / name), "destination": relative, "sha256": checksum, "executable": False})
        copies.append("COPY " + json.dumps([relative, "/" + name]))

    installer_destination = "installer/" + installer["name"]

    artifacts.append({"path": str(pool / installer["name"]), "destination": installer_destination, "sha256": installer["sha256"], "executable": False})
    copies.append("COPY " + json.dumps([installer_destination, "/" + installer_destination]))
    (template.parent / "Dockerfile.selected").write_text(contents.replace(marker, "\n".join(copies)))
    (destination / "provider-core-stream.json").write_text(json.dumps(artifacts, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("check", "acquire", "capture"))
    parser.add_argument("--pin", type=pathlib.Path, required=True)
    parser.add_argument("--runtime-lock", type=pathlib.Path, required=True)
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--destination", type=pathlib.Path)
    args = parser.parse_args()

    if args.action == "check":
        check_release(args.pin, args.runtime_lock, args.pool)
        print("complete")
    elif args.action == "acquire":
        acquire_release(args.pin, args.runtime_lock, args.pool)
    else:
        if args.destination is None:
            parser.error("capture requires --destination")

        capture_image_inputs(args.pin, args.runtime_lock, args.pool, args.destination)


if __name__ == "__main__":
    main()
