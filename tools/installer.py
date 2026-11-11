"""Run the SDK-pinned installer without importing an image's bundled pip.

The issued directory contains this driver, its release record and the pip wheel.
An explicit wheel path lets offline image preparation consume the same cache
input. Capture and verify those bytes before Python can import them. Keeping
execution in this interpreter preserves target-venv identity and borrowed locks.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys
import tempfile


def unique_fields(pairs):
    record = {}
    for name, value in pairs:
        if name in record:
            raise ValueError("Cannot start the installer: release record contains a duplicate field.")
        record[name] = value
    return record


def release(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Cannot start the installer: release record must be a regular file.")
        record = json.load(stream, object_pairs_hook=unique_fields)
    if not (
        type(record) is dict and set(record) == {"name", "sha256", "size", "url"}
        and type(record["name"]) is str
        and re.fullmatch(r"pip-[0-9]+\.[0-9]+(?:\.[0-9]+)?-py3-none-any\.whl", record["name"])
        and type(record["sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        and type(record["size"]) is int and record["size"] > 0
        and type(record["url"]) is str
        and record["url"].startswith("https://files.pythonhosted.org/")
        and record["url"].endswith("/" + record["name"])
    ):
        raise ValueError("Cannot start the installer: release record must identify one pip wheel by name, size, SHA-256 and HTTPS source.")
    return record


def wheel_bytes(wheel, record):
    """Read and authenticate the bounded input before it can become executable."""
    descriptor = os.open(wheel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Cannot start the installer: its wheel must be a regular file.")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("action", choices=("pip", "module"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    record = release(directory / "release.json")
    wheel = directory / record["name"]
    if args.wheel is not None:
        wheel = args.wheel
    module = "pip"
    arguments = args.arguments
    if args.action == "module":
        if not arguments:
            parser.error("module requires the Python module to run")
        module, *arguments = arguments
    else:
        arguments = ["--isolated", *arguments]
    if "pip" in sys.modules:
        raise RuntimeError("Cannot start the installer: pip was imported before its release wheel was verified.")
    with tempfile.TemporaryDirectory(prefix="provider-installer-") as temporary:
        captured = Path(temporary) / record["name"]
        capture(wheel, record, captured)
        sys.path.insert(0, str(captured))
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
