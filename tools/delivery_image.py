"""Acquire and capture the pinned shared-delivery release in the common wheelhouse."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from tools.artifacts import regular_file, require_download_proxy
from tools.provider_core_acquire import acquire_tool, tool_present
from tools.wheelhouse import digest, pool_lock, read_json, requirements, select_wheels


def release_inputs(pin, runtime_lock):
    release = read_json(pin)
    (release_version, wheel_digest) = (release["version"], release["wheel_sha256"])
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release_version) or not re.fullmatch(
        r"[0-9a-f]{64}", wheel_digest
    ):
        raise ValueError("Cannot use delivery release: invalid pinned version or wheel hash.")

    with regular_file(runtime_lock) as source:
        runtime_requirements = source.read().decode("utf-8")

    install_lock_bytes = (
        runtime_requirements.rstrip()
        + f"\nprovider-core=={release_version} \\\n    --hash=sha256:{wheel_digest}\n"
    ).encode()
    if hashlib.sha256(install_lock_bytes).hexdigest() != release["install_lock_sha256"]:
        raise ValueError(
            "Cannot use delivery release: runtime lock differs from the issued install lock."
        )
    return release, install_lock_bytes



def release_wheels(release):
    provider_wheel_name = f"provider_core-{release['version']}-py3-none-any.whl"
    return dict(release["runtime_wheels"]) | {provider_wheel_name: release["wheel_sha256"]}



def missing_wheels(release, pool):
    if not pool.is_dir() or pool.is_symlink():
        raise ValueError("Cannot use delivery release: wheelhouse must be an existing directory.")

    missing_wheel_ids = []
    for (wheel_name, expected_sha256) in release_wheels(release).items():
        if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", wheel_name) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise ValueError("Cannot use delivery release: malformed pinned wheel identity.")

        wheel_path = pool / wheel_name
        if wheel_path.exists() or wheel_path.is_symlink():
            if digest(wheel_path) != expected_sha256:
                raise ValueError(
                    f"Cannot reuse {wheel_name}: cached bytes differ from "
                    "the pinned release; existing file retained."
                )
        else:
            missing_wheel_ids.append((wheel_name, expected_sha256))
    return missing_wheel_ids



def publish_release_wheels(tool, pool, artifacts):
    from pip._vendor.packaging.utils import parse_wheel_filename

    with tempfile.TemporaryDirectory(prefix="delivery-lock-") as temporary:
        acquisition_lock = Path(temporary) / "pylock.delivery.toml"
        with acquisition_lock.open("x", encoding="utf-8") as stream:
            stream.write('lock-version = "1.0"\ncreated-by = "nmr-provider-tools delivery"\n')
            for (wheel_name, checksum, url) in artifacts:
                (distribution, version, _, _) = parse_wheel_filename(wheel_name)
                stream.write("[[packages]]\n")
                stream.write(
                    f"name = {json.dumps(str(distribution))}\n"
                    f"version = {json.dumps(str(version))}\n"
                )
                stream.write(f"[packages.archive]\nurl = {json.dumps(url)}\n")
                stream.write(f"[packages.archive.hashes]\nsha256 = {json.dumps(checksum)}\n")

        subprocess.run(
            [
                sys.executable, "-I", "-B", str(tool),
                "acquire", "--lock", str(acquisition_lock), "--pool", str(pool),
            ],
            check=True,
        )



def acquire_release(pin, runtime_lock, pool, tool):
    (release, _) = release_inputs(pin, runtime_lock)
    with pool_lock(pool):
        missing = missing_wheels(release, pool)

    artifacts = []
    for (wheel_name, expected_sha256) in missing:
        require_download_proxy()
        if wheel_name.startswith("provider_core-"):
            url = f"https://github.com/numpde/nmr-provider-tools/releases/download/v{release['version']}/{wheel_name}"
        else:
            (distribution, version, *_) = wheel_name.split("-")
            with urllib.request.urlopen(
                f"https://pypi.org/pypi/{distribution}/{version}/json",
                timeout=30,
            ) as response:
                metadata = json.load(response)
            candidates = [
                item["url"]
                for item in metadata["urls"]
                if item["filename"] == wheel_name and item["digests"]["sha256"] == expected_sha256
            ]
            if len(candidates) != 1 or urllib.parse.urlsplit(candidates[0]).hostname != "files.pythonhosted.org":
                raise ValueError(
                    f"Cannot acquire {wheel_name}: the pinned wheel is absent from its published index."
                )
            url = candidates[0]

        artifacts.append((wheel_name, expected_sha256, url))

    if artifacts:
        publish_release_wheels(tool, pool, artifacts)



def capture_image_inputs(pin, runtime_lock, pool, destination):
    with pool_lock(pool) as lock_fd:
        _capture_image_inputs(pin, runtime_lock, pool, destination, lock_fd)


def _capture_image_inputs(pin, runtime_lock, pool, destination, lock_fd):
    (release, lock_bytes) = release_inputs(pin, runtime_lock)
    lock = destination / "provider-core-install.lock"
    with lock.open("xb") as target:
        target.write(lock_bytes)
    lock.chmod(0o644)
    requirements(lock)
    if missing_wheels(release, pool):
        raise ValueError("Cannot build delivery: the selected wheelhouse is missing pinned release wheels.")
    selected = select_wheels(lock, pool, lock_fd=lock_fd)
    if selected["files"] != release_wheels(release):
        raise ValueError("Cannot build delivery: selected wheels differ from the pinned release closure.")
    identities = destination / "provider-core-wheel-inputs.json"
    with identities.open("x", encoding="utf-8") as target:
        json.dump(selected, target, sort_keys=True)
        target.write("\n")
    identities.chmod(0o644)
    template = destination / "containers/delivery/Dockerfile"
    marker = "# selected-wheel-inputs"
    contents = template.read_text()
    if contents.count(marker) != 1:
        raise ValueError("Cannot build delivery: the Dockerfile has no unique wheel input marker.")
    copies = "\n".join("COPY " + json.dumps(["wheelhouse/" + name, "/" + name])
                       for name in sorted(selected["files"]))
    generated = destination / "containers/delivery/Dockerfile.selected"
    with generated.open("x") as target:
        target.write(contents.replace(marker, copies))
    generated.chmod(0o644)
    manifest = destination / "provider-core-stream.json"
    with manifest.open("x", encoding="utf-8") as target:
        json.dump([{"directory": "wheelhouse", "filename": name, "sha256": checksum}
                   for (name, checksum) in sorted(selected["files"].items())], target, sort_keys=True)
        target.write("\n")
    manifest.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "acquire", "capture"))
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--tool-pin", type=Path)
    args = parser.parse_args()
    if args.action == "check":
        (release, _) = release_inputs(args.pin, args.runtime_lock)
        with pool_lock(args.pool):
            if missing_wheels(release, args.pool) or (args.tool_pin and not tool_present(args.pool, read_json(args.tool_pin))):
                print("missing")
            else:
                print("complete")
    elif args.action == "acquire":
        if args.tool_pin is None:
            parser.error("acquire requires --tool-pin")
        tool = acquire_tool(args.pool, read_json(args.tool_pin))
        acquire_release(args.pin, args.runtime_lock, args.pool, tool)
    else:
        if args.destination is None:
            parser.error("capture requires --destination")
        capture_image_inputs(args.pin, args.runtime_lock, args.pool, args.destination)
