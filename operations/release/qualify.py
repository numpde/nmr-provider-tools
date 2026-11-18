"""Qualify the exact GitHub release artifacts, including their source rebuild.

Follows pay-to-play-wire 4c92a3b tools/ops/package.py. The pre-backend source
inventory is independent of setuptools' file list; tests run against installed
wheels with only source-distribution test files on their test path.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
import pathlib
from pathlib import PurePosixPath

from tools.artifacts import regular_file
from build_bundle import build as build_wheelhouse_bundle
from build_acquisition_bundle import build as build_acquisition_bundle
from sdk_build.core_environment import behavioral_install_lock, build_candidate, install_environment, wheelhouse_source_inventory
from sdk_build.core_environment import SDK_TYPE_REQUIREMENTS, TYPE_REQUIREMENTS_SOURCE
from tools.wheelhouse import digest, requirements
from type_requirements import require_type


def artifact_names(version):
    prefix = f"provider_core-{version}"

    return {prefix + suffix for suffix in ("-py3-none-any.whl", ".tar.gz", "-install.lock", "-native-install.lock", "-release.json", "-sha256.json")} | {f"wheelhouse-tool-{version}.pyz", f"acquisition-owner-{version}.tar.gz"}


def qualify_wheelhouse_bundle(bundle, scratch):
    locked_requirements = requirements(pathlib.Path("/wheelhouse-tool.lock"))

    (packaging_version, accepted_hashes) = next(
        (version, hashes)
        for ((name, version), hashes) in locked_requirements.items()
        if name == "packaging"
    )

    packaging_wheel_name = f"packaging-{packaging_version}-py3-none-any.whl"
    packaging_digest = digest(pathlib.Path("/wheelhouse") / packaging_wheel_name)

    if not (packaging_digest in accepted_hashes):
        raise ValueError("Cannot qualify wheelhouse bundle: cached packaging wheel differs from its lock.")

    selection_lock = scratch / "pylock.toml"

    selection_lock.write_text(
        'lock-version = "1.0"\n'
        'created-by = "release qualification"\n'
        '[[packages]]\n'
        'name = "packaging"\n'
        f'version = "{packaging_version}"\n'
        '[[packages.wheels]]\n'
        f'url = "https://example.invalid/{packaging_wheel_name}"\n'
        f'hashes = {{ sha256 = "{packaging_digest}" }}\n'
    )

    projected_lock = scratch / "pylock.local.toml"

    wheelhouse_command = [
        "python3",  # Use the qualification container's base interpreter
        "-I",  # Ignore PYTHON* settings and the user site
        "-S",  # Skip site initialization and installed dependencies
        str(bundle),  # Load dependencies from the issued zipapp
    ]

    subprocess.run(
        [
            *wheelhouse_command,  # Exercise the issued zipapp in isolation
            "locate",  # Load the artifact lookup command
            "--help",  # Verify that the command loads without fetching packages
        ],
        check=True,  # Reject a bundle that cannot load the command
        stdout=subprocess.DEVNULL,  # Keep successful help text out of the release log
    )

    selected_wheels = subprocess.run(
        [
            *wheelhouse_command,  # Resolve with the issued zipapp
            "select",  # Compute the artifact selection without downloading
            "--lock",  # Read selection inputs from a lock
            str(selection_lock),  # Use the one-wheel qualification fixture
        ],
        check=True,  # Reject failed selection before reading its output
        stdout=subprocess.PIPE,  # Capture the selected artifacts for comparison
        text=True,  # Read the JSON selection as text
    )

    if json.loads(selected_wheels.stdout)["artifacts"][0]["sha256"] != packaging_digest:
        raise ValueError("Cannot qualify wheelhouse bundle: target selection differs from the lock.")

    subprocess.run(
        [
            *wheelhouse_command,  # Verify artifacts with the issued zipapp
            "check",  # Verify the selected wheel in the local pool
            "--lock",  # Select the expected artifact and hash
            str(selection_lock),  # Use the same fixture as the selection check
            "--pool",  # Select the source of cached wheel bytes
            "/wheelhouse",  # Read the mounted release wheelhouse
            "--local-lock",  # Generate a lock that points to local files
            str(projected_lock),  # Keep the projection for independent inspection
            "--local-root",  # Set the artifact path as seen by the consumer
            "/wheels",  # Use the eventual installation mount path
        ],
        check=True,  # Reject a missing or mismatched wheel
        stdout=subprocess.DEVNULL,  # Inspect the projected file directly
    )

    projected_artifact = tomllib.loads(projected_lock.read_text())["packages"][0]["wheels"][0]
    projected_hashes = projected_artifact.get("hashes")

    if projected_artifact.get("path") != "/wheels/" + packaging_wheel_name or "url" in projected_artifact:
        raise ValueError("Cannot qualify wheelhouse bundle: projected artifact differs from the selected lock.")

    require_type(projected_hashes, dict)

    if projected_hashes.get("sha256") != packaging_digest:
        raise ValueError("Cannot qualify wheelhouse bundle: projected artifact differs from the selected lock.")

    dockerfile = scratch / "Dockerfile"

    dockerfile.write_text("FROM scratch\n")
    manifest = scratch / "stream-manifest.json"
    report = scratch / "pip-report.json"

    report.write_text(
        json.dumps(
            {
                "version": "1",
                "install": [
                    {
                        "download_info": {
                            "url": "file:///wheelhouse/" + packaging_wheel_name,
                            "archive_info": {"hashes": {"sha256": packaging_digest}},
                        }
                    }
                ],
            }
        )
    )

    report_conversion = subprocess.run(
        [
            *wheelhouse_command,  # Parse the report with the issued zipapp
            "report",  # Convert pip's selection into a build-input manifest
            "--report",  # Identify the pip report to convert
            str(report),  # Use the fixture for the selected wheel
            "--pool",  # Require the reported wheel to come from this pool
            "/wheelhouse",  # Resolve the fixture's local wheel path
            "--directory",  # Select the wheel's destination in the context
            "wheels",  # Place wheels under the context's wheels directory
        ],
        check=True,  # Reject report conversion failure
        stdout=subprocess.PIPE,  # Capture the generated manifest
        text=True,  # Keep the result available for JSON parsing and writing
    )

    expected_report = [
        {"filename": packaging_wheel_name, "sha256": packaging_digest, "directory": "wheels"}
    ]

    if json.loads(report_conversion.stdout) != expected_report:
        raise ValueError("Cannot qualify wheelhouse bundle: pip report projection differs from the selected artifact.")

    manifest.write_text(report_conversion.stdout)

    manifest_identity = subprocess.run(
        [
            *wheelhouse_command,  # Compute identity with the issued zipapp
            "identity",  # Hash the canonical build manifest
            "--manifest",  # Select the manifest to hash
            str(manifest),  # Use the result of the report conversion
        ],
        check=True,  # Reject identity computation failure
        stdout=subprocess.PIPE,  # Capture the hash for independent comparison
        text=True,  # Read the hash as text
    ).stdout.strip()

    canonical_manifest = (
        json
        .dumps(
            json.loads(report_conversion.stdout),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        .encode()
    )

    if manifest_identity != hashlib.sha256(canonical_manifest).hexdigest():
        raise ValueError("Cannot qualify wheelhouse bundle: selection identity differs from the selected artifact.")

    source_allowlist = scratch / "stream-sources.json"

    source_allowlist.write_text(
        json.dumps(
            [
                {
                    "path": str(dockerfile),
                    "destination": "Dockerfile",
                    "sha256": digest(dockerfile),
                    "executable": False,
                }
            ]
        )
    )

    streamed_context = subprocess.run(
        [
            *wheelhouse_command,  # Build the context with the issued zipapp
            "stream",  # Emit the build context as a tar stream
            "--pool",  # Select the source of the manifest's wheel bytes
            "/wheelhouse",  # Read wheels from the mounted cache
            "--source-allowlist",  # Limit checkout input to explicitly selected files
            str(source_allowlist),  # Admit the fixture Dockerfile and its hash
            "--manifest",  # Limit wheels to the selected names and hashes
            str(manifest),  # Reuse the independently checked wheel selection
        ],
        check=True,  # Reject a failed context stream
        stdout=subprocess.PIPE,  # Preserve tar bytes for inspection
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(streamed_context)) as archive:
        if set(archive.getnames()) != {"Dockerfile", "wheels/" + packaging_wheel_name}:
            raise ValueError("Cannot qualify wheelhouse bundle: streamed inputs differ from the selected context.")

        with archive.extractfile("wheels/" + packaging_wheel_name) as streamed_wheel:
            if hashlib.file_digest(streamed_wheel, "sha256").hexdigest() != packaging_digest:
                raise ValueError("Cannot qualify wheelhouse bundle: streamed wheel differs from the lock.")


def qualify_acquisition_bundle(bundle, scratch, expected_source_files):
    """Validate the pinned source inventory before extracting or importing code."""

    require_type(expected_source_files, dict)

    if not (expected_source_files):
        raise ValueError("Cannot qualify acquisition source bundle: missing source inventory.")

    with tarfile.open(bundle, "r:gz") as archive:
        verified_sources = {}

        for member in archive.getmembers():
            member_name = member.name
            member_path = PurePosixPath(member_name)

            if (
                (not member_name) or
                (not member_path.parts) or
                member_path.is_absolute() or
                (".." in member_path.parts) or
                (member_path.as_posix() != member_name) or
                (not member.isfile())
            ):
                raise ValueError(f"{member_name=} should be a regular file at a canonical relative path within the acquisition archive")

            if not (member_name not in verified_sources):
                raise ValueError(f"{member_name=} should appear only once in the acquisition archive")

            expected_member = expected_source_files.get(member_name)

            require_type(expected_member, dict)

            member_fields = set(expected_member)
            expected_member_fields = {"mode", "sha256", "size"}

            if not (member_fields == expected_member_fields):
                raise ValueError(f"For {member_name=}, {member_fields=} should match {expected_member_fields=}")

            expected_member_size = expected_member["size"]
            expected_member_mode = expected_member["mode"]

            if not (member.size == expected_member_size):
                raise ValueError(f"For {member_name=}, {member.size=} should match {expected_member_size=}")

            if not (member.mode == expected_member_mode):
                raise ValueError(f"For {member_name=}, {member.mode=} should match {expected_member_mode=}")

            with archive.extractfile(member) as reader:
                member_bytes = reader.read()

            member_sha256 = hashlib.sha256(member_bytes).hexdigest()
            expected_member_sha256 = expected_member["sha256"]

            if not (member_sha256 == expected_member_sha256):
                raise ValueError(f"For {member_name=}, {member_sha256=} should match {expected_member_sha256=}")

            verified_sources[member_name] = member_bytes

    if not (verified_sources.keys() == expected_source_files.keys()):
        missing_members = sorted(expected_source_files.keys() - verified_sources.keys(), key=repr)
        unexpected_members = sorted(verified_sources.keys() - expected_source_files.keys())

        raise ValueError(f"Acquisition archive members should match the source inventory, but {missing_members=} and {unexpected_members=}")

    for member_name in verified_sources:
        for parent_path in PurePosixPath(member_name).parents:
            parent_name = parent_path.as_posix()

            if not (parent_name not in verified_sources):
                raise ValueError(f"{parent_name=} should be a directory for {member_name=}, but it is an archive file")

    destination = scratch / "acquisition-source"

    destination.mkdir()

    for (name, member_bytes) in verified_sources.items():
        extracted_file = destination / name

        extracted_file.parent.mkdir(parents=True, exist_ok=True)

        with extracted_file.open("xb") as output:
            output.write(member_bytes)

    subprocess.run(
        [
            "bash",  # Parse the owner with its actual shell
            "-n",  # Check shell syntax without executing the owner
            str(destination / "acquisition_tool/owner.sh"),  # Read the extracted owner script
        ],
        check=True,  # Reject a bundle whose owner cannot be parsed
    )

    subprocess.run(
        [
            "python3",  # Use the qualification container's base interpreter
            "-I",  # Ignore PYTHON* settings and the user site
            "-S",  # Skip installed dependencies
            "-B",  # Keep bytecode out of the extracted tree
            str(destination / "acquisition_tool/source_update.py"),  # Run the issued source verifier
            "verify",  # Compare the extracted files with their manifest
            "--source-dir",  # Select the tree to check
            str(destination),  # Verify the actual extraction
    ],
        check=True,  # Reject any manifest or source mismatch
    )

    cold_import_check = pathlib.Path(__file__).resolve().parents[2] / "packages/acquisition/build/acquisition_import_check.py"

    subprocess.run(
        [
            "python3",  # Use the qualification container's base interpreter
            "-I",  # Ignore PYTHON* settings and the user site
            "-S",  # Exclude site packages from the import check
            "-B",  # Keep bytecode out of the extracted tree
            str(cold_import_check),  # Test the acquisition modules' import closure
            str(destination),  # Supply the extracted bundle as the only added import root
        ],
        check=True,  # Reject imports that need an unbundled dependency
    )

    subprocess.run(
        [
            "python3",  # Use the qualification container's base interpreter
            "-I",  # Ignore PYTHON* settings and the user site
            "-S",  # Exclude site packages from the CLI check
            "-B",  # Keep bytecode out of the extracted tree
            str(destination / "acquisition_tool/identity.py"),  # Load the issued identity CLI
            "--help",  # Exercise startup without contacting Docker
        ],
        check=True,  # Reject an identity CLI that cannot start
        stdout=subprocess.DEVNULL,  # Keep successful help text out of the release log
    )


def unpack_source(archive, destination, version):
    """Admit the complete tar inventory before creating any extracted file."""
    root_name = f"provider_core-{version}"

    with tarfile.open(archive, "r:gz") as source_archive:
        members = source_archive.getmembers()
        member_names = set()

        for member in members:
            member_path = PurePosixPath(member.name)

            if (
                (member.name in member_names) or
                (not member.name) or
                member_path.is_absolute() or
                (".." in member_path.parts) or
                (member_path.as_posix() != member.name) or
                (member_path.parts[0] != root_name) or
                (not (member.isfile() or member.isdir())) or
                (member.mode & 0o022)
            ):
                raise ValueError(
                    "Cannot qualify package: source archive has duplicate, "
                    "unsafe or unsupported members."
                )

            member_names.add(member.name)

        for member in members:
            extracted_path = destination / member.name

            if member.isdir():
                extracted_path.mkdir(parents=True, exist_ok=True)
            else:
                extracted_path.parent.mkdir(parents=True, exist_ok=True)

                with source_archive.extractfile(member) as reader, extracted_path.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)

                extracted_path.chmod(member.mode & 0o777)

    return destination / root_name


def source_payload(source, inventory):
    require_type(inventory, dict)

    if not (inventory):
        raise ValueError("Cannot qualify package: the pre-build source inventory is missing.")

    for (name, facts) in inventory.items():
        member_path = PurePosixPath(name)

        if (
            member_path.is_absolute() or
            (".." in member_path.parts) or
            (member_path.as_posix() != name) or
            (not (
                (name in {"README.md", "MANIFEST.in", "pyproject.toml"}) or
                name.startswith(("src/provider_core/", "tests/"))
            ))
        ):
            raise ValueError(
                "Cannot qualify package: the recorded source inventory "
                "has unsupported entries."
            )

        require_type(facts, dict)

        if set(facts) != {"sha256", "size", "mode"}:
            raise ValueError(
                "Cannot qualify package: the recorded source inventory "
                "has unsupported entries."
            )

        source_file = source / name

        if (
            (not source_file.is_file()) or
            (digest(source_file) != facts["sha256"]) or
            (source_file.stat().st_size != facts["size"]) or
            (source_file.stat().st_mode & 0o777 != facts["mode"])
        ):
            raise ValueError(
                "Cannot qualify package: source distribution differs from "
                "its pre-build source inventory."
            )

    generated_metadata_files = {"PKG-INFO", "setup.cfg"} | {
        "src/provider_core.egg-info/" + name
        for name in (
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "requires.txt",
            "top_level.txt",
        )
    }

    observed_files = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file()
    }

    if observed_files != set(inventory) | generated_metadata_files:
        raise ValueError(
            "Cannot qualify package: source distribution has missing "
            "or unrecorded authored files."
        )

    return {
        name.removeprefix("src/"): (source / name).read_bytes()
        for name in inventory
        if name.startswith("src/provider_core/")
    }


def wheel_payload(wheel, expected, version):
    with zipfile.ZipFile(wheel) as archive:
        archive_members = archive.namelist()
        metadata_prefix = f"provider_core-{version}.dist-info/"

        if (
            (len(archive_members) != len(set(archive_members))) or
            any(
                (
                    not name.startswith(metadata_prefix) and name not in expected
                    for name in archive_members
                ),
            )
        ):
            raise ValueError("Cannot qualify package: wheel contains duplicate or unexpected payload files.")

        wheel_files = {
            name: archive.read(name)
            for name in archive_members
            if not name.startswith(metadata_prefix)
        }

        if wheel_files != expected:
            raise ValueError(
                "Cannot qualify package: installed wheel payload differs "
                "from its captured source files."
            )

        return {
            name.removeprefix(metadata_prefix): archive.read(name)
            for name in archive_members
            if name.startswith(metadata_prefix) and not name.endswith("/RECORD")
        }


def qualify_installed(wheel, lock, source, pool, root, suites):
    # The wheel and runtime-install lock remain exactly as issued.
    # Behavioral tests get the source archive's own test lock; contract/native proofs do not.
    runtime_python = install_environment(
        root / "runtime",
        lock,
        pool,
        extra_pools=(wheel.parent,),
        required_files={wheel.name: wheel},
    )

    behavioral_python = None

    if any(suite in ("unit", "integration") for suite in suites):
        behavioral_lock = behavioral_install_lock(
            lock,
            source / "tests/requirements.lock",
            root / "behavioral-install.lock",
        )

        behavioral_python = install_environment(
            root / "behavioral",
            behavioral_lock,
            pool,
            extra_pools=(wheel.parent,),
            required_files={wheel.name: wheel},
        )

    test_root = root / "run"

    shutil.copytree(source / "tests", test_root / "tests")

    for suite in suites:
        selected_python = behavioral_python if suite in ("unit", "integration") else runtime_python

        test_command = [
            str(selected_python),  # Use the environment containing the candidate wheel
            "-I",  # Ignore PYTHON* settings and the user site
            "-B",  # Keep bytecode out of the staged suite
            str(test_root / "tests/run_suite.py"),  # Run the tests issued in the source distribution
            suite,  # Select the qualification suite for this install
            "test*.py",  # Run the full suite without a filename filter
        ]

        test_process = subprocess.run(
            test_command,  # Run the installed SDK qualification suite
            cwd=test_root,  # Resolve fixtures relative to the staged suite
        )

        if test_process.returncode:
            raise SystemExit(
                f"Installed provider-core {suite} release qualification failed "
                f"(exit {test_process.returncode}). See the suite diagnostic above "
                f"and the retained release-test.log."
            )


def check(artifacts, revision):
    with tempfile.TemporaryDirectory(prefix="package-proof-") as temporary:
        scratch = pathlib.Path(temporary)

        for artifact in artifacts.iterdir():
            with regular_file(artifact):
                pass

        captured_wheels = list(artifacts.glob("*.whl"))

        if len(captured_wheels) != 1:
            raise ValueError("Cannot qualify package: expect exactly one provider-core wheel.")

        wheel_filename = re.fullmatch(
            r"provider_core-((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-py3-none-any.whl",
            captured_wheels[0].name,
        )

        if wheel_filename is None:
            raise ValueError("Cannot qualify package: wheel name must identify a provider-core release version.")

        release_version = wheel_filename[1]
        artifact_prefix = f"provider_core-{release_version}"

        if {artifact.name for artifact in artifacts.iterdir()} != artifact_names(release_version):
            raise ValueError("Cannot qualify package: expect exactly the declared release artifacts.")

        checksum_manifest_path = artifacts / (artifact_prefix + "-sha256.json")
        original_manifest_digest = digest(checksum_manifest_path)
        recorded_hashes = json.loads(checksum_manifest_path.read_text())

        observed_hashes = {
            artifact.name: digest(artifact)
            for artifact in artifacts.iterdir()
            if not artifact.name.endswith("-sha256.json")
        }

        if recorded_hashes != observed_hashes:
            raise ValueError("Cannot qualify package: captured release hashes do not match the declared artifacts.")

        release_record = json.loads((artifacts / (artifact_prefix + "-release.json")).read_text())

        if (
            (release_record["source_revision"] != revision) or
            (release_record["files"] != (
                {
                    name: checksum
                    for (name, checksum) in observed_hashes.items()
                    if not name.endswith("-release.json")
                }
            ))
        ):
            raise ValueError(
                "Cannot qualify package: source revision or recorded artifact "
                "identities differ from this release."
            )

        if release_record["tool_source_files"] != wheelhouse_source_inventory(
            pathlib.Path("/workspace/wheelhouse_tool"),
            type_requirements_source=TYPE_REQUIREMENTS_SOURCE,
        ):
            raise ValueError("Cannot qualify package: wheelhouse tool source differs from the release inputs.")

        wheelhouse_bundle = artifacts / f"wheelhouse-tool-{release_version}.pyz"
        rebuilt_wheelhouse_bundle = scratch / wheelhouse_bundle.name

        build_wheelhouse_bundle(
            pathlib.Path("/wheelhouse"),
            rebuilt_wheelhouse_bundle,
            pathlib.Path("/workspace"),
            pathlib.Path("/wheelhouse-tool.lock"),
            type_requirements_source=TYPE_REQUIREMENTS_SOURCE,
        )

        if wheelhouse_bundle.read_bytes() != rebuilt_wheelhouse_bundle.read_bytes():
            raise ValueError("Cannot qualify package: wheelhouse bundle differs from its source rebuild.")

        qualify_wheelhouse_bundle(wheelhouse_bundle, scratch)

        acquisition_bundle = artifacts / f"acquisition-owner-{release_version}.tar.gz"
        rebuilt_acquisition_bundle = scratch / acquisition_bundle.name

        acquisition_record = build_acquisition_bundle(
            pathlib.Path("/wheelhouse"),
            rebuilt_acquisition_bundle,
            pathlib.Path("/workspace"),
            captured_wheels[0],
            pathlib.Path("/acquisition-bootstrap.pylock.toml"),
            revision=revision,
            wheelhouse_bundle=wheelhouse_bundle,
        )

        if (
            (acquisition_bundle.read_bytes() != rebuilt_acquisition_bundle.read_bytes()) or
            (release_record["acquisition_source_files"] != acquisition_record["source_files"]) or
            (release_record["acquisition_dependency_inputs"] != acquisition_record["dependency_inputs"])
        ):
            raise ValueError(
                "Cannot qualify acquisition source bundle: bytes or provenance "
                "differ from the pinned rebuild."
            )

        qualify_acquisition_bundle(acquisition_bundle, scratch, acquisition_record["source_files"])

        extracted_source = unpack_source(
            artifacts / (artifact_prefix + ".tar.gz"),
            scratch / "source",
            release_version,
        )

        source_project = tomllib.loads((extracted_source / "pyproject.toml").read_text())

        if (
            (source_project["project"]["name"] != "provider-core") or
            (source_project["project"]["version"] != release_version)
        ):
            raise ValueError("Cannot qualify package: source project and version differ from the release artifacts.")

        source_python_files = source_payload(extracted_source, release_record["source_files"])

        if (extracted_source / SDK_TYPE_REQUIREMENTS).read_bytes() != TYPE_REQUIREMENTS_SOURCE.read_bytes():
            raise ValueError("The SDK source distribution should contain the captured type requirements module.")

        original_wheel_metadata = wheel_payload(captured_wheels[0], source_python_files, release_version)

        runtime_install_lock = artifacts / (artifact_prefix + "-install.lock")

        if requirements(runtime_install_lock) != requirements(pathlib.Path("/core-runtime.lock")) | {
            ("provider-core", release_version): {digest(captured_wheels[0])}
        }:
            raise ValueError(
                "Cannot qualify package: issued install lock differs from "
                "the closed runtime dependencies and wheel."
            )

        native_install_lock = artifacts / (artifact_prefix + "-native-install.lock")

        if requirements(native_install_lock) != {
            ("provider-core", release_version): {digest(captured_wheels[0])}
        }:
            raise ValueError(
                "Cannot qualify package: native install lock differs from "
                "the same released wheel."
            )

        rebuild_output = scratch / "rebuilt"

        rebuild_output.mkdir()

        (rebuilt_wheel, rebuilt_lock, rebuilt_dependency_inputs) = build_candidate(
            extracted_source,
            pathlib.Path("/core-runtime.lock"),
            pathlib.Path("/core-build.lock"),
            pathlib.Path("/wheelhouse"),
            rebuild_output,
        )

        if (
            (rebuilt_dependency_inputs != release_record["dependency_inputs"]) or
            (wheel_payload(rebuilt_wheel, source_python_files, release_version) != original_wheel_metadata)
        ):
            raise ValueError(
                "Cannot qualify package: source rebuild metadata or dependency "
                "inputs differ from the original wheel."
            )

        runtime_root = pathlib.Path("/runtime/package-proof")

        runtime_root.mkdir()

        qualify_installed(
            captured_wheels[0],
            runtime_install_lock,
            extracted_source,
            pathlib.Path("/wheelhouse"),
            runtime_root / "original",
            ("unit", "contract", "integration"),
        )

        qualify_installed(
            rebuilt_wheel,
            rebuilt_lock,
            extracted_source,
            pathlib.Path("/wheelhouse"),
            runtime_root / "rebuilt",
            ("contract",),
        )

        qualify_installed(
            captured_wheels[0],
            native_install_lock,
            extracted_source,
            pathlib.Path("/wheelhouse"),
            runtime_root / "native",
            ("native",),
        )

        if (
            (digest(checksum_manifest_path) != original_manifest_digest) or
            ({
                artifact.name: digest(artifact)
                for artifact in artifacts.iterdir()
                if not artifact.name.endswith("-sha256.json")
             }
             != observed_hashes)
        ):
            raise ValueError("Cannot qualify package: release artifacts changed during qualification.")

        for (name, checksum) in sorted(
            (observed_hashes | {checksum_manifest_path.name: original_manifest_digest}).items()
        ):
            print("QUALIFIED_SHA256", checksum, name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--artifacts", type=pathlib.Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    check(args.artifacts, args.revision)
