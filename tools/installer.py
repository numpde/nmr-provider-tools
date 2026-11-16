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
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def unique_fields(field_pairs):
    """Reject duplicate JSON object keys before constructing the dictionary."""
    field_counts = Counter(key for (key, _) in field_pairs)
    duplicate_fields = field_counts - Counter(field_counts.keys())

    if duplicate_fields:
        raise ValueError(f"Cannot start the installer: release record contains {duplicate_fields=}")

    return dict(field_pairs)


def release(path):
    """Read the SDK identity without accepting symlinks or duplicate JSON fields."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    with os.fdopen(descriptor, "rb") as stream:
        if not (stat.S_ISREG(os.fstat(stream.fileno()).st_mode)):
            raise ValueError("Cannot start the installer: release record must be a regular file.")

        record = json.load(stream, object_pairs_hook=unique_fields)

    return validate_release(record)


def validate_release(record):
    """Validate metadata before using its filename or byte count."""

    if not (type(record) is (expected_type := dict)):
        raise TypeError(f"{type(record)=} ≠ {expected_type=}")

    if not (set(record) == {"name", "sha256", "size", "url"}):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    if not (type((record_name := record["name"])) is (expected_type := str)):
        raise TypeError(f"{type(record_name)=} ≠ {expected_type=}")

    if not (re.fullmatch(r"pip-[0-9]+\.[0-9]+(?:\.[0-9]+)?-py3-none-any\.whl", record["name"])):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    if not (type((record_sha256 := record["sha256"])) is (expected_type := str)):
        raise TypeError(f"{type(record_sha256)=} ≠ {expected_type=}")

    if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    if not (type((record_size := record["size"])) is (expected_type := int)):
        raise TypeError(f"{type(record_size)=} ≠ {expected_type=}")

    if not (record["size"] > 0):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    if not (type((record_url := record["url"])) is (expected_type := str)):
        raise TypeError(f"{type(record_url)=} ≠ {expected_type=}")

    if not (record["url"].startswith("https://files.pythonhosted.org/") and record["url"].endswith("/" + record["name"])):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")

    return record


def wheel_bytes(wheel, record):
    """Read and authenticate the bounded input before it can become executable."""
    descriptor = os.open(wheel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    with os.fdopen(descriptor, "rb") as source:
        if not (stat.S_ISREG(os.fstat(source.fileno()).st_mode)):
            raise ValueError("Cannot start the installer: its wheel must be a regular file.")

        return checked_bytes(source, record)


def checked_bytes(source, record):
    """Bound the read and authenticate its bytes before pip can execute them."""
    data = source.read(record["size"] + 1)

    if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError(
            "Cannot start the installer: wheel size or SHA-256 differs from the SDK release."
        )

    return data


def capture(wheel, record, destination):
    """Admit a private copy, so shared-cache replacement cannot rebind imports."""
    data = wheel_bytes(wheel, record)

    with destination.open("xb") as target:
        target.write(data)


def install(pool, record):
    """Bootstrap from authenticated bytes, without executing the inherited pip."""

    with tempfile.TemporaryDirectory(prefix="provider-installer-") as temporary:
        captured = Path(temporary) / record["name"]

        capture(pool / record["name"], record, captured)

        subprocess.run(
            [sys.executable,
             "-I",
             "-B",
             str(captured / "pip"),
             "--isolated",
             "install",
             "--no-index",
             "--no-deps",
             "--no-compile",
             str(captured)],
            check=True,
        )


def pip_command(python):
    """Run the target's pinned installer in the shared-pool lock holder."""

    return [str(python), "-I", "-B", "-m", "pip", "--isolated"]


def create_environment(destination):
    """Seed an environment from image-owned pip, without running ensurepip.

    Pip is toolchain input separate from the application's installation lock.
    Subsequent operations use the target's own pip so its process can retain
    borrowed shared-pool lock descriptors without an intermediate dispatcher.
    """
    image = Path("/opt/provider-tools/installer")
    record = release(image / "release.json")
    python = destination / "bin/python"

    with tempfile.TemporaryDirectory(prefix="provider-installer-") as temporary:
        captured = Path(temporary) / record["name"]

        capture(image / "wheels" / record["name"], record, captured)
        subprocess.run([sys.executable, "-I", "-B", "-m", "venv", "--without-pip", str(destination)], check=True)

        subprocess.run(
            [sys.executable,
             "-I",
             "-B",
             "-m",
             "pip",
             "--isolated",
             "--python",
             str(python),
             "install",
             "--no-index",
             "--no-deps",
             "--no-cache-dir",
             "--no-compile",
             str(captured)],
            check=True,
        )

    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    args = parser.parse_args()

    install(args.pool, release(args.record))
