"""Acquire and capture the pinned shared-delivery release in the common wheelhouse."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import re
import reprlib
import subprocess
import sys
import tempfile
import urllib.parse
import pathlib

from tools.artifacts import regular_file, require_download_proxy
from tools.adapters.github_release import release_asset_url
from tools.adapters.pip_install import hashed_requirement
from tools.adapters.pylock import write_wheel_archive_lock
from tools.adapters import pypi_project_json as pypi_requests
from tools.installer import release as installer_release
from sdk_build.provider_core_acquire import acquire_tool, tool_present
from tools.wheelhouse import digest, pool_lock, read_json, requirements, select_wheels
from tools.validation.upstream import pypi_project_json as pypi_release


def release_inputs(pin, runtime_lock):
    release = read_json(pin)
    (release_version, wheel_digest) = (release["version"], release["wheel_sha256"])

    if (
        (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release_version)) or
        (not re.fullmatch(r"[0-9a-f]{64}", wheel_digest))
    ):
        raise ValueError("Cannot use delivery release: invalid pinned version or wheel hash.")

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


def release_wheels(release):
    provider_wheel_name = f"provider_core-{release['version']}-py3-none-any.whl"

    return dict(release["runtime_wheels"]) | {provider_wheel_name: release["wheel_sha256"]}


def missing_wheels(release, pool):
    if (not pool.is_dir()) or (pool.is_symlink()):
        raise ValueError("Cannot use delivery release: wheelhouse must be an existing directory.")

    missing_wheel_ids = []

    for (wheel_name, expected_sha256) in release_wheels(release).items():
        if (
            (not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", wheel_name)) or
            (not re.fullmatch(r"[0-9a-f]{64}", expected_sha256))
        ):
            raise ValueError("Cannot use delivery release: malformed pinned wheel identity.")

        wheel_path = pool / wheel_name

        if wheel_path.exists() or wheel_path.is_symlink():
            wheel_sha256 = digest(wheel_path)

            if not (wheel_sha256 == expected_sha256):
                raise ValueError(
                    f"For {wheel_name=}, {wheel_sha256=} should match {expected_sha256=}. "
                    f"The existing file was retained."
                )
        else:
            missing_wheel_ids.append((wheel_name, expected_sha256))

    return missing_wheel_ids


def publish_release_wheels(tool, pool, artifacts):
    with tempfile.TemporaryDirectory(prefix="delivery-lock-") as temporary:
        acquisition_lock = pathlib.Path(temporary) / "pylock.delivery.toml"

        with acquisition_lock.open("x", encoding="utf-8") as stream:
            write_wheel_archive_lock(stream, artifacts, created_by="nmr-provider-tools delivery")

        subprocess.run(
            [
                sys.executable,  # Use the worker's Python interpreter
                "-I",  # Ignore PYTHON* settings and the user site
                "-B",  # Suppress bytecode writes
                str(tool),  # Run the pinned wheelhouse zipapp
                "acquire",  # Fetch missing artifacts and verify cached copies
                "--lock",  # Select artifacts from an explicit lock
                str(acquisition_lock),  # Use the generated delivery lock
                "--pool",  # Select the shared artifact cache
                str(pool),  # Reuse or publish artifacts in this directory
            ],
            check=True,  # Propagate publisher failure
        )


def acquire_release(pin, runtime_lock, pool, tool):
    (release, _) = release_inputs(pin, runtime_lock)

    with pool_lock(pool):
        missing = missing_wheels(release, pool)

    artifacts = []

    for (wheel_name, expected_sha256) in missing:
        require_download_proxy()

        if wheel_name.startswith("provider_core-"):
            tag = "v" + release["version"]
            url = release_asset_url("numpde/nmr-provider-tools", tag, wheel_name)
        else:
            (distribution, version, *_) = wheel_name.split("-")

            metadata = pypi_requests.release_metadata(distribution, version, timeout_seconds=30)

            candidates = [
                pypi_release.file_url(release_file)
                for release_file in pypi_release.release_files(metadata)
                if
                (pypi_release.file_name(release_file) == wheel_name) and
                (pypi_release.file_sha256(release_file) == expected_sha256)
            ]

            matching_downloads = len(candidates)

            if not (matching_downloads == 1):
                raise ValueError(f"For {wheel_name=} and {expected_sha256=}, {matching_downloads=} should be one")

            url = candidates[0]
            download_host = urllib.parse.urlsplit(url).hostname
            expected_download_host = "files.pythonhosted.org"

            if not (download_host == expected_download_host):
                raise ValueError(f"For {wheel_name=}, {reprlib.repr(download_host)=} should match {expected_download_host=}")

        artifacts.append((wheel_name, expected_sha256, url))

    if artifacts:
        publish_release_wheels(tool, pool, artifacts)


def capture_image_inputs(pin, runtime_lock, pool, destination):
    with pool_lock(pool) as lock_fd:
        _capture_image_inputs(pin, runtime_lock, pool, destination, lock_fd)


def _capture_image_inputs(pin, runtime_lock, pool, destination, lock_fd):
    (release, install_lock_bytes) = release_inputs(pin, runtime_lock)
    captured_lock = destination / "provider-core-install.lock"

    with captured_lock.open("xb") as target:
        target.write(install_lock_bytes)

    captured_lock.chmod(0o644)
    requirements(captured_lock)

    if missing_wheels(release, pool):
        raise ValueError("Cannot build delivery: the selected wheelhouse is missing pinned release wheels.")

    selected_wheels = select_wheels(captured_lock, pool, lock_fd=lock_fd)

    if selected_wheels["files"] != release_wheels(release):
        raise ValueError("Cannot build delivery: selected wheels differ from the pinned release closure.")

    wheel_input_record = destination / "provider-core-wheel-inputs.json"

    with wheel_input_record.open("x", encoding="utf-8") as target:
        json.dump(selected_wheels, target, sort_keys=True)
        print(file=target)

    wheel_input_record.chmod(0o644)

    dockerfile_template = destination / "operations/delivery/image/Dockerfile"
    marker = "# selected-wheel-inputs"
    template_contents = dockerfile_template.read_text()

    if template_contents.count(marker) != 1:
        raise ValueError("Cannot build delivery: the Dockerfile has no unique wheel input marker.")

    copy_instructions = "\n".join(
        "COPY " + json.dumps(["wheelhouse/" + name, "/" + name])
        for name in sorted(selected_wheels["files"])
    )

    installer = installer_release(destination / "requirements/installer-release.json")

    copy_instructions += (
        "\n"
        "COPY " + json.dumps(["installer/" + installer["name"], "/installer/" + installer["name"]])
    )

    selected_dockerfile = destination / "operations/delivery/image/Dockerfile.selected"

    with selected_dockerfile.open("x") as target:
        target.write(template_contents.replace(marker, copy_instructions))

    selected_dockerfile.chmod(0o644)

    stream_manifest = destination / "provider-core-stream.json"

    with stream_manifest.open("x", encoding="utf-8") as target:
        json.dump(
            [
                {"directory": "wheelhouse", "filename": name, "sha256": checksum}
                for (name, checksum) in sorted(selected_wheels["files"].items())
            ]
            + [{"directory": "installer", "filename": installer["name"], "sha256": installer["sha256"]}],
            target,
            sort_keys=True,
        )

        print(file=target)

    stream_manifest.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("check", "acquire", "capture"))
    parser.add_argument("--pin", type=pathlib.Path, required=True)
    parser.add_argument("--runtime-lock", type=pathlib.Path, required=True)
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--destination", type=pathlib.Path)
    parser.add_argument("--tool-pin", type=pathlib.Path)
    args = parser.parse_args()

    if args.action == "check":
        (release, _) = release_inputs(args.pin, args.runtime_lock)

        with pool_lock(args.pool):
            if (
                missing_wheels(release, args.pool) or
                ((args.tool_pin) and (not tool_present(args.pool, read_json(args.tool_pin))))
            ):
                print("missing")
            else:
                print("complete")
    elif args.action == "acquire":
        if args.tool_pin is None:
            parser.error("acquire requires --tool-pin")

        acquisition_tool = acquire_tool(args.pool, read_json(args.tool_pin))

        acquire_release(args.pin, args.runtime_lock, args.pool, acquisition_tool)
    else:
        if args.destination is None:
            parser.error("capture requires --destination")

        capture_image_inputs(args.pin, args.runtime_lock, args.pool, args.destination)
