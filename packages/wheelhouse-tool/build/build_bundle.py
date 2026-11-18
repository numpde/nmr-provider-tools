"""Build the shared wheelhouse zipapp from its pinned pure-Python wheels."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import io
import os
import tempfile
import zipfile
import pathlib

from tools.wheelhouse import requirements

# Bundle construction and release provenance describe the same authored modules.
# repo-check: allow-module-constant
SOURCE_MODULES = (
    "__init__.py",
    "__main__.py",
    "stream.py",
    "report.py",
    "identity.py",
    "locate.py",
    "network.py",
)

def dependency_files(pool, lock):
    files = {}

    for ((package, version), accepted_hashes) in requirements(lock).items():
        wheel_name = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
        wheel_bytes = (pool / wheel_name).read_bytes()
        wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()

        if not (wheel_sha256 in accepted_hashes):
            expected_sha256s = sorted(accepted_hashes)
            raise ValueError(f"For {wheel_name=}, {wheel_sha256=} should be one of {expected_sha256s=}")

        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    if member.filename in files:
                        raise ValueError(f"Cannot build wheelhouse bundle: duplicate {member.filename}.")

                    files[member.filename] = archive.read(member)

    return files


def build(pool, output, source, lock, *, type_requirements_source):
    bundle_members = dependency_files(pool, lock)

    bundle_members["__main__.py"] = (
        b"import sys\n"
        b"if sys.argv[1:2] == ['stream']:\n"
        b"    del sys.argv[1]\n"
        b"    from wheelhouse_tool.stream import main\n"
        b"elif sys.argv[1:2] == ['report']:\n"
        b"    del sys.argv[1]\n"
        b"    from wheelhouse_tool.report import main\n"
        b"elif sys.argv[1:2] == ['identity']:\n"
        b"    del sys.argv[1]\n"
        b"    from wheelhouse_tool.identity import main\n"
        b"elif sys.argv[1:2] == ['locate']:\n"
        b"    del sys.argv[1]\n"
        b"    from wheelhouse_tool.locate import main\n"
        b"else:\n"
        b"    from wheelhouse_tool.__main__ import main\n"
        b"raise SystemExit(main())\n"
    )

    for name in SOURCE_MODULES:
        bundle_members[f"wheelhouse_tool/{name}"] = (source / "wheelhouse_tool" / name).read_bytes()

    bundle_members["type_requirements.py"] = type_requirements_source.read_bytes()

    (temporary_fd, temporary_name) = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".part",
        dir=output.parent,
    )

    try:
        with os.fdopen(temporary_fd, "wb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for (name, member_bytes) in sorted(bundle_members.items()):
                    zip_entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    zip_entry.compress_type = zipfile.ZIP_STORED
                    zip_entry.external_attr = 0o644 << 16

                    archive.writestr(zip_entry, member_bytes)

            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())

        os.link(temporary_name, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)

        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--lock", type=pathlib.Path, required=True)
    args = parser.parse_args()

    package = pathlib.Path(__file__).resolve().parents[1]
    build(
        args.pool,
        args.output,
        package / "src",
        args.lock,
        type_requirements_source=package.parent / "type-requirements/src/type_requirements.py",
    )


if __name__ == "__main__":
    main()
