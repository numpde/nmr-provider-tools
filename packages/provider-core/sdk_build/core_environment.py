"""Build the shared wheel and install its closed hash lock without networking.

The pip transaction follows nmrnet-repro d09b77b tools/core_environment.py and
provider_core_tests.py. Consumers install the issued wheel; only this owner
builds it. A fixed ZIP epoch makes equal source bytes produce equal wheel bytes.
"""

from tools.container_guard import require_container

require_container()

import argparse
import gzip
import json
import os
import re
import reprlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import pathlib

from tools.validation.upstream import pip_installation_report
from tools.validation.upstream.pip_requirements import parse_lock_line

from tools.adapters.pip_install import (
    pip_command,
    build_wheel_command,
    check_environment_command,
    hashed_requirement,
    install_locked_wheels_command,
)

from tools.artifacts import fsync_directory, regular_file
from tools.artifact_metadata import metadata_mismatch
from tools.installer import create_environment
from tools.wheelhouse import digest, pool_read_lock, read_json, required_wheel_constraints, requirements, select_wheels, wheel_inputs_from_report
from type_requirements import require_type


# These paths identify the canonical build input and its SDK package destination
# repo-check: allow-module-constant
TYPE_REQUIREMENTS_SOURCE = pathlib.Path("/workspace/packages/type-requirements/src/type_requirements.py")

# repo-check: allow-module-constant
SDK_TYPE_REQUIREMENTS = pathlib.Path("src/provider_core/_type_requirements.py")


# Keep ordinary release identifiers complete while bounding rejected metadata
# repo-check: allow-module-constant
_BUILD_EVIDENCE = reprlib.Repr()

_BUILD_EVIDENCE.maxstring = 256


def build_source_wheel(
    *,
    source: pathlib.Path,
    staged_source: pathlib.Path,
    python: pathlib.Path,
    output: pathlib.Path,
    bundled_sources: dict[pathlib.Path, pathlib.Path] | None = None,
) -> pathlib.Path:
    """Build from writable scratch, never from a read-only source mount.

    The caller owns staged_source's temporary parent and its cleanup. Returning
    the stage permits a subsequent source-distribution build from the same tree.
    bundled_sources maps package destinations to their canonical source files.
    """

    if not source.is_dir():
        raise ValueError(f"{source=} should be a directory")

    if source.is_symlink():
        raise ValueError(f"{source=} should not be a symlink")

    for source_entry in source.rglob("*"):
        if source_entry.is_symlink():
            raise ValueError(f"{source_entry=} should not be a symlink")

    resolved_staged_source = staged_source.resolve()
    resolved_source = source.resolve()

    if resolved_staged_source.is_relative_to(resolved_source):
        raise ValueError(f"{resolved_staged_source=} should be outside {resolved_source=}")

    shutil.copytree(source, staged_source)

    for directory in (staged_source, *staged_source.rglob("*")):
        if directory.is_dir():
            directory.chmod(directory.stat().st_mode | stat.S_IWUSR)

    if bundled_sources is not None:
        for (destination, canonical_source) in bundled_sources.items():
            shutil.copy2(canonical_source, staged_source / destination)

    subprocess.run(
        build_wheel_command(python, staged_source=staged_source, output=output),
        check=True,  # Stop if the wheel build fails
        env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},  # Fix wheel timestamps across rebuilds
    )

    return staged_source


# silent-defaults: allow-contract -- Installation accepts explicit optional immutable extra wheel pools; an omitted pool is the empty collection, while missing required files has a separate None sentinel.
def install_locked(python, lock, wheels, report, extra_pools=(), required_files=None, lock_fd=None):
    borrowed_descriptors = ()

    if lock_fd is not None:
        borrowed_descriptors = (lock_fd,)

    with required_wheel_constraints(required_files) as constraints:
        subprocess.run(
            install_locked_wheels_command(python, lock, wheels, report, extra_pools, constraints=constraints),
            check=True,  # Reject an incomplete installation
            pass_fds=borrowed_descriptors,  # Keep shared pool protection alive in pip
        )

    return wheel_inputs_from_report(lock, wheels, report, extra_pools, required_files)


def native_requirements(wheel, runtime_lock, pool):
    """Let pip derive the native dependency subset from the issued wheel metadata."""
    locked = requirements(runtime_lock)

    with tempfile.TemporaryDirectory(prefix="native-selection-") as temporary:
        scratch = pathlib.Path(temporary)
        constraints = scratch / "constraints.txt"

        constraints.write_text(
            "".join(
                f"{name}=={version}\n"  # machine-string: pip constraints require distribution pins
                for (name, version) in locked
            )
        )

        report = scratch / "report.json"

        subprocess.run(
            [
                *pip_command(sys.executable),
                "install",
                "--dry-run",
                "--ignore-installed",
                "--no-index",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--quiet",
                "--find-links",
                str(pool),
                "--constraint",
                str(constraints),
                "--report",
                str(report),
                str(wheel),
            ],
            check=True,
        )

        selected = {}

        for distribution in pip_installation_report.install_records(read_json(report)):
            metadata = distribution["metadata"]
            distribution_pin = f"{metadata['name']}=={metadata['version']}"  # machine-string: pip requirement syntax supplies the distribution name and version
            identity = parse_lock_line(distribution_pin.encode())

            # silent-defaults: allow-contract -- The built SDK wheel supplies its own digest; dependency hashes come from the trusted runtime lock
            hashes = {digest(wheel)} if identity[0] == "provider-core" else locked[identity]

            selected_hash = pip_installation_report.archive_sha256(distribution)

            if not (selected_hash in hashes):
                raise ValueError(f"{identity=}; {selected_hash=} should be in {hashes=}")

            selected[identity] = hashes

    return selected


def build_candidate(source, runtime_lock, build_lock, pool, output):
    """Freeze locks and select shared-pool wheels before building a release."""

    with pool_read_lock(pool) as lock_fd:
        return _build_candidate(source, runtime_lock, build_lock, pool, output, lock_fd)


def _build_candidate(source, runtime_lock, build_lock, pool, output, lock_fd):
    if output.is_symlink():
        raise ValueError(f"{output=} should not be a symlink")

    if not output.is_dir():
        raise ValueError(f"{output=} should be a directory")

    existing_output_entry = next(output.iterdir(), None)

    if existing_output_entry is not None:
        raise ValueError(f"{existing_output_entry=} should be absent from {output=}")

    with tempfile.TemporaryDirectory(prefix="core-build-") as temporary:
        scratch = pathlib.Path(temporary)
        identities = {}
        locks = {}

        for (role, lock) in (("runtime", runtime_lock), ("build", build_lock)):
            frozen = scratch / f"{role}.lock"  # machine-string: temporary lock filename selected by the build role

            shutil.copyfile(lock, frozen)
            requirements(frozen)
            identities[role] = select_wheels(frozen, pool, lock_fd=lock_fd)
            locks[role] = frozen

        project_metadata = tomllib.loads((source / "pyproject.toml").read_text())
        release_version = project_metadata["project"]["version"]

        project_name = project_metadata["project"]["name"]
        expected_project_name = "provider-core"

        if not (project_name == expected_project_name):
            raise ValueError(f"{_BUILD_EVIDENCE.repr(project_name)=} should match {expected_project_name=}")

        require_type(release_version, str)

        release_version_pattern = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"

        if not re.fullmatch(release_version_pattern, release_version):
            raise ValueError(f"{_BUILD_EVIDENCE.repr(release_version)=} should match {release_version_pattern=}")

        builder = scratch / "builder"
        builder_python = create_environment(builder)

        installed_build_inputs = install_locked(
            python=builder_python,
            lock=locks["build"],
            wheels=pool,
            report=scratch / "build-install.json",
            lock_fd=lock_fd,
        )

        if installed_build_inputs != identities["build"]:
            raise ValueError(f"{builder_python=}: " + metadata_mismatch(installed_build_inputs, identities["build"]))

        staged_package = build_source_wheel(
            source=source,
            staged_source=scratch / "package",
            python=builder_python,
            output=output,
            bundled_sources={SDK_TYPE_REQUIREMENTS: TYPE_REQUIREMENTS_SOURCE},
        )

        sdist_builder = pathlib.Path(__file__).with_name("build_sdist.py")

        subprocess.run(
            [
                str(builder_python),  # Use the environment with the pinned backend
                "-I",  # Ignore PYTHON* settings and the user site
                "-B",  # Suppress bytecode writes
                str(sdist_builder),  # Invoke the source distribution backend
                str(output),  # Write the archive alongside the candidate wheel
            ],
            cwd=staged_package,  # Give the backend the staged project
            env=os.environ | {"SOURCE_DATE_EPOCH": "315532800"},  # Fix build timestamps
            check=True,  # Stop if the source distribution build fails
        )

        wheel = output / f"provider_core-{release_version}-py3-none-any.whl"  # machine-string: wheel filename consumed by installers
        sdist = output / f"provider_core-{release_version}.tar.gz"  # machine-string: source archive filename consumed by publishers

        built_artifacts = set(output.iterdir())
        expected_artifacts = {wheel, sdist}

        if built_artifacts != expected_artifacts:
            raise ValueError(f"{output=}: " + metadata_mismatch(built_artifacts, expected_artifacts))

        for distribution in (wheel, sdist):
            if not distribution.is_file():
                raise ValueError(f"{distribution=} should be a regular file")

            if distribution.is_symlink():
                raise ValueError(f"{distribution=} should not be a symlink")

        normalize_sdist(sdist)

        install_lock = output / f"provider_core-{release_version}-install.lock"  # machine-string: lock filename consumed by the install environment
        runtime_lock_text = locks["runtime"].read_text()

        requirements(locks["runtime"])

        install_lock.write_text(
            runtime_lock_text.rstrip()
            + ("\n" + hashed_requirement("provider-core", release_version, digest(wheel)))
        )

        expected_requirements = requirements(locks["runtime"]) | {
            ("provider-core", release_version): {digest(wheel)}
        }

        observed_requirements = requirements(install_lock)

        if observed_requirements != expected_requirements:
            raise ValueError(f"{install_lock=}: " + metadata_mismatch(observed_requirements, expected_requirements))

        native_lock = output / f"provider_core-{release_version}-native-install.lock"  # machine-string: lock filename consumed by native verification

        expected_native_requirements = native_requirements(wheel, locks["runtime"], pool)

        native_lock.write_text(
            "".join(
                (
                    f"{name}=={version} \\\n"  # machine-string: pip hash-lock requirement with a continued line
                    + " \\\n".join(
                        f"    --hash=sha256:{checksum}"  # machine-string: pip's allowed archive hash option
                        for checksum in sorted(hashes)
                    )
                    + "\n"
                )
                for ((name, version), hashes) in sorted(expected_native_requirements.items())
            )
        )

    return (wheel, install_lock, identities)


# silent-defaults: allow-contract -- An optional caller-supplied wheel pool is immutable and empty by default; the expected-inputs and required-files None values signal distinct checks.
def install_environment(root, lock, pool, expected_inputs=None, extra_pools=(), required_files=None):
    """Install one complete application lock using the image's approved pip."""

    with pool_read_lock(pool) as lock_fd:
        return _install_environment(root, lock, pool, expected_inputs, extra_pools, required_files, lock_fd)


def _install_environment(root, lock, pool, expected_inputs, extra_pools, required_files, lock_fd):
    environment_python = create_environment(root / "environment")

    selected_wheel_inputs = select_wheels(
        lock,
        pool,
        extra_pools,
        required_files,
        lock_fd,
    )

    if (expected_inputs is not None) and (selected_wheel_inputs != expected_inputs):
        raise ValueError(f"{lock=}: " + metadata_mismatch(selected_wheel_inputs, expected_inputs))

    with tempfile.NamedTemporaryFile(prefix="core-install-report-", suffix=".json") as report:
        installed_wheel_inputs = install_locked(
            python=environment_python,
            lock=lock,
            wheels=pool,
            report=pathlib.Path(report.name),
            extra_pools=extra_pools,
            required_files=required_files,
            lock_fd=lock_fd,
        )

        if installed_wheel_inputs != selected_wheel_inputs:
            raise ValueError(f"{environment_python=}: " + metadata_mismatch(installed_wheel_inputs, selected_wheel_inputs))

    subprocess.run(check_environment_command(environment_python), check=True)
    shutil.copyfile(lock, root / "provider-core-install.lock")

    return environment_python


def behavioral_install_lock(runtime_lock, test_lock, destination):
    """Combine the exact installed runtime wheel lock with test-only wheels.

    Issued runtime and native locks are never mutated. Consumers receive a
    temporary complete lock, and duplicate package identities are refused.
    """
    runtime_requirements = requirements(runtime_lock)
    test_requirements = requirements(test_lock)
    overlapping_requirements = set(runtime_requirements).intersection(test_requirements)

    if overlapping_requirements:
        overlapping_requirement_list = sorted(overlapping_requirements)

        raise ValueError(f"{overlapping_requirement_list=} should be empty")

    destination.write_text(
        runtime_lock.read_text().rstrip() + "\n" + test_lock.read_text().strip() + "\n"
    )

    merged_requirements = requirements(destination)
    expected_merged_requirements = runtime_requirements | test_requirements

    if merged_requirements != expected_merged_requirements:
        raise ValueError(f"{destination=}: " + metadata_mismatch(merged_requirements, expected_merged_requirements))

    return destination


def source_inventory(source):
    inventory = {}

    for source_entry in sorted(source.rglob("*")):
        if source_entry.is_symlink():
            raise ValueError(f"{source_entry=} should not be a symlink")

        if source_entry.is_file():
            inventory[source_entry.relative_to(source).as_posix()] = source_file_identity(source_entry)
        elif not source_entry.is_dir():
            raise ValueError(f"{source_entry=} should be a regular file or directory")

    return inventory


def source_file_identity(source_file):
    source_stat = source_file.stat()

    return {
        "sha256": digest(source_file),
        "size": source_stat.st_size,
        "mode": source_stat.st_mode & 0o777,
    }


def wheelhouse_source_inventory(source, *, type_requirements_source):
    from build_bundle import source_modules

    inventory = {name: source_file_identity(source / name) for name in source_modules(source)}
    inventory["type_requirements.py"] = source_file_identity(type_requirements_source)

    return inventory


def normalize_sdist(path):
    """Remove host account metadata and use the same fixed epoch as the wheel."""

    # Keep replacement on the same filesystem, with automatic failure cleanup.
    with tempfile.TemporaryDirectory(prefix="sdist-normalize-", dir=path.parent) as temporary:
        normalized_path = pathlib.Path(temporary) / "archive.tar.gz"

        with tarfile.open(path, "r:gz") as source_archive, normalized_path.open("xb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=315532800) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as normalized_archive:
                    for member in sorted(source_archive.getmembers(), key=lambda member: member.name):
                        if not (member.isfile() or member.isdir()):
                            raise ValueError(f"{_BUILD_EVIDENCE.repr(member.name)=} should be a regular file or directory; {member.type=}")

                        member.uid = member.gid = 0
                        member.uname = member.gname = ""
                        member.mtime = 315532800
                        member.pax_headers = {}

                        if member.isfile():
                            with source_archive.extractfile(member) as member_content:
                                normalized_archive.addfile(member, member_content)
                        else:
                            normalized_archive.addfile(member)

        normalized_path.replace(path)


def release(source, runtime_lock, build_lock, pool, output, revision):
    from build_bundle import build as build_wheelhouse_bundle, dependency_files
    from build_acquisition_bundle import build as build_acquisition_bundle

    expected_revision_pattern = r"[0-9a-f]{40}"

    if not re.fullmatch(expected_revision_pattern, revision):
        raise ValueError(f"{_BUILD_EVIDENCE.repr(revision)=} should match {expected_revision_pattern=}")

    dependency_files(pool, pathlib.Path("/requirements/wheelhouse-tool.lock"))
    source_files = source_inventory(source)
    source_files[SDK_TYPE_REQUIREMENTS.as_posix()] = source_file_identity(TYPE_REQUIREMENTS_SOURCE)

    (wheel, install_lock, dependency_inputs) = build_candidate(
        source=source,
        runtime_lock=runtime_lock,
        build_lock=build_lock,
        pool=pool,
        output=output,
    )

    artifact_prefix = wheel.name.removesuffix("-py3-none-any.whl")
    sdist = output / (artifact_prefix + ".tar.gz")
    native_lock = output / (artifact_prefix + "-native-install.lock")

    wheelhouse_bundle = output / ("wheelhouse-tool-" + artifact_prefix.removeprefix("provider_core-") + ".pyz")

    build_wheelhouse_bundle(
        pool=pool,
        output=wheelhouse_bundle,
        source=pathlib.Path("/workspace"),
        lock=pathlib.Path("/requirements/wheelhouse-tool.lock"),
        type_requirements_source=TYPE_REQUIREMENTS_SOURCE,
    )

    acquisition_bundle = output / ("acquisition-owner-" + artifact_prefix.removeprefix("provider_core-") + ".tar.gz")

    acquisition_record = build_acquisition_bundle(
        pool=pool,
        output=acquisition_bundle,
        source=pathlib.Path("/workspace"),
        core_wheel=wheel,
        pin=pathlib.Path("/requirements/acquisition-bootstrap.pylock.toml"),
        revision=revision,
        wheelhouse_bundle=wheelhouse_bundle,
    )

    release_files = (wheel, sdist, install_lock, native_lock, wheelhouse_bundle, acquisition_bundle)
    release_record = output / (artifact_prefix + "-release.json")

    release_record.write_text(
        json.dumps(
            {
                "source_revision": revision,
                "dependency_inputs": dependency_inputs,
                "source_files": source_files,
                "tool_source_files": wheelhouse_source_inventory(
                    pathlib.Path("/workspace/wheelhouse_tool"),
                    type_requirements_source=TYPE_REQUIREMENTS_SOURCE,
                ),
                "acquisition_source_files": acquisition_record["source_files"],
                "acquisition_dependency_inputs": acquisition_record["dependency_inputs"],
                "files": {artifact.name: digest(artifact) for artifact in release_files},
            },
            sort_keys=True,
            indent=2,
        ) + "\n"
    )

    checksum_manifest = output / (artifact_prefix + "-sha256.json")

    checksum_manifest.write_text(
        json.dumps(
            {artifact.name: digest(artifact) for artifact in (*release_files, release_record)},
            sort_keys=True,
            indent=2,
        ) + "\n"
    )

    for artifact in (*release_files, release_record, checksum_manifest):
        with regular_file(artifact) as stream:
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())

    fsync_directory(output)
    print(json.dumps({"source_revision": revision, "wheel": wheel.name, "sha256": digest(wheel)}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("action", choices=("release", "install"))
    parser.add_argument("--pool", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--lock", type=pathlib.Path)
    parser.add_argument("--source", type=pathlib.Path)
    parser.add_argument("--runtime-lock", type=pathlib.Path)
    parser.add_argument("--build-lock", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--revision")
    parser.add_argument("--expected-inputs", type=pathlib.Path)
    args = parser.parse_args()

    if args.action == "install":
        if (args.root is None) or (args.lock is None):
            parser.error("install requires --root and --lock")

        expected_inputs = None

        if args.expected_inputs is not None:
            expected_inputs = read_json(args.expected_inputs)

        install_environment(args.root, args.lock, args.pool, expected_inputs)
    else:
        if any((argument is None) for argument in (args.source, args.runtime_lock, args.build_lock, args.output, args.revision)):
            parser.error("release requires --source, --runtime-lock, --build-lock, --output and --revision")

        release(args.source, args.runtime_lock, args.build_lock, args.pool, args.output, args.revision)


if __name__ == "__main__":
    main()
