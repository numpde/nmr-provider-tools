"""Own the approved installer for image assembly and new Python environments.

Verification uses only the standard library. The consuming Dockerfile owns this
build step; ordinary operations use the resulting image's installed pip.
"""

from .container_guard import require_container

require_container()

import argparse
from collections import Counter
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

from type_requirements import require_type


def duplicate_json_fields(field_pairs: list[tuple[str, object]]) -> Counter[str]:
    """Count occurrences beyond the first for each repeated JSON field."""
    field_counts = Counter(field_name for (field_name, _) in field_pairs)

    return field_counts - Counter(field_counts.keys())


def unique_fields(field_pairs):
    """Reject duplicate JSON object keys before constructing the dictionary."""
    duplicate_fields = sorted(duplicate_json_fields(field_pairs))

    if duplicate_fields:
        raise ValueError(f"Installer release record should have unique JSON fields, but {duplicate_fields=}")

    return dict(field_pairs)


def release(release_json_path):
    """Read the SDK identity without accepting symlinks or duplicate JSON fields."""
    release_json_fd = os.open(release_json_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    with os.fdopen(release_json_fd, "rb") as stream:
        if not stat.S_ISREG(observed_stat_mode := os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{release_json_path=} has {observed_stat_mode=}")

        record = json.load(stream, object_pairs_hook=unique_fields)

    return validate_release(record)


def validate_release(record):
    """Validate metadata before using its filename or byte count."""

    require_type(record, dict)

    if not (set(record) == {"name", "sha256", "size", "url"}):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    record_name = record["name"]
    require_type(record_name, str)

    if not re.fullmatch(pattern := r"pip-[0-9]+\.[0-9]+(?:\.[0-9]+)?-py3-none-any\.whl", record_name):
        raise ValueError(f"Cannot start the installer: {record_name=} does not follow {pattern=}.")

    record_sha256 = record["sha256"]
    require_type(record_sha256, str)

    if not re.fullmatch((expected_pattern := r"[0-9a-f]{64}"), record_sha256):
        raise ValueError(f"{record_sha256=} should match {expected_pattern=}")

    require_type(record["size"], int)

    if not (record["size"] > 0):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    require_type(record["url"], str)

    if not (record["url"].startswith("https://files.pythonhosted.org/") and record["url"].endswith("/" + record["name"])):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    return record


def wheel_bytes(wheel, record):
    """Read and authenticate the bounded input before it can become executable."""
    descriptor = os.open(wheel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(observed_stat_mode := os.fstat(source.fileno()).st_mode):
            raise ValueError(f"Cannot start the installer: {observed_stat_mode=}")

        return checked_bytes(source, record)


def checked_bytes(source, record):
    """Bound the read and authenticate its bytes before pip can execute them."""
    wheel_name = record["name"]
    expected_size = record["size"]
    captured_bytes = source.read(expected_size + 1)
    captured_size = len(captured_bytes)

    if captured_size != expected_size:
        raise ValueError(f"{wheel_name=}, {captured_size=} ≠ {expected_size=}")

    wheel_sha256 = hashlib.sha256(captured_bytes).hexdigest()
    expected_sha256 = record["sha256"]

    if wheel_sha256 != expected_sha256:
        raise ValueError(f"{wheel_name=}, {wheel_sha256=} ≠ {expected_sha256=}")

    return captured_bytes


def capture(wheel, record, destination):
    """Admit a private copy, so shared-cache replacement cannot rebind imports."""
    captured_bytes = wheel_bytes(wheel, record)

    with destination.open("xb") as target:
        target.write(captured_bytes)


def install(pool, record):
    """Bootstrap from authenticated bytes, without executing the inherited pip."""

    with tempfile.TemporaryDirectory(prefix="provider-installer-") as temporary:
        captured = pathlib.Path(temporary) / record["name"]

        capture(pool / record["name"], record, captured)

        subprocess.run(
            [
                sys.executable,  # Run with the current image's Python
                "-I",  # Ignore PYTHON* settings and the user site
                "-B",  # Suppress bytecode writes during bootstrap
                str(captured / "pip"),  # Execute the authenticated pip wheel's entry point
                "--isolated",  # Ignore user pip configuration and environment overrides
                "install",  # Install the approved pip into the current image
                "--no-index",  # Keep installer bootstrap offline
                "--no-deps",  # Install the approved wheel alone
                "--no-compile",  # Omit generated bytecode from the installed files
                str(captured),  # Install the verified private copy of the wheel
            ],
            check=True,  # Stop image assembly if installer bootstrap fails
        )


def pip_command(python):
    """Run the target's pinned installer in the shared-pool lock holder."""

    return [
        str(python),  # Use the target environment's interpreter
        "-I",  # Ignore PYTHON* settings and the user site
        "-B",  # Suppress bytecode writes
        "-m",  # Resolve pip within the target interpreter
        "pip",  # Use the target's approved installer
        "--isolated",  # Ignore user pip configuration and environment overrides
    ]


def create_environment(destination):
    """Seed an environment from image-owned pip, without running ensurepip.

    Pip is toolchain input separate from the application's installation lock.
    Subsequent operations use the target's own pip so its process can retain
    borrowed shared-pool lock descriptors without an intermediate dispatcher.
    """
    image = pathlib.Path("/opt/provider-tools/installer")
    record = release(image / "release.json")
    python = destination / "bin/python"

    with tempfile.TemporaryDirectory(prefix="provider-installer-") as temporary:
        captured = pathlib.Path(temporary) / record["name"]

        capture(image / "wheels" / record["name"], record, captured)
        subprocess.run([sys.executable, "-I", "-B", "-m", "venv", "--without-pip", str(destination)], check=True)

        subprocess.run(
            [
                sys.executable,  # Run the image's approved installer
                "-I",  # Ignore PYTHON* settings and the user site
                "-B",  # Suppress bytecode writes during bootstrap
                "-m",  # Resolve pip in the image's Python environment
                "pip",  # Use the already installed, approved toolchain
                "--isolated",  # Ignore user pip configuration and environment overrides
                "--python",  # Direct installation into a different environment
                str(python),  # Seed the new isolated environment
                "install",  # Install its own copy of pip
                "--no-index",  # Keep environment creation offline
                "--no-deps",  # Install the approved wheel alone
                "--no-cache-dir",  # Use the verified private wheel instead of a cache
                "--no-compile",  # Omit generated bytecode from the installed files
                str(captured),  # Supply the exact installer wheel bytes
            ],
            check=True,  # Return an interpreter only after pip is installed
        )

    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--record", type=pathlib.Path, required=True)
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    args = parser.parse_args()

    install(args.pool, release(args.record))
