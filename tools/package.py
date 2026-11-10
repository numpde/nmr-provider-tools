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
from pathlib import Path, PurePosixPath

from tools.artifacts import regular_file
from tools.build_wheelhouse_bundle import build as build_wheelhouse_bundle
from tools.build_acquisition_bundle import build as build_acquisition_bundle
from tools.core_environment import behavioral_install_lock, build_candidate, install_environment, wheelhouse_source_inventory
from tools.wheelhouse import digest, requirements


def artifact_names(version):
    prefix = f"provider_core-{version}"
    return {prefix + suffix for suffix in ("-py3-none-any.whl", ".tar.gz", "-install.lock", "-native-install.lock", "-release.json", "-sha256.json")} | {f"wheelhouse-tool-{version}.pyz", f"acquisition-owner-{version}.tar.gz"}


def qualify_wheelhouse_bundle(bundle, scratch):
    locked_requirements = requirements(Path("/wheelhouse-tool.lock"))
    (packaging_version, accepted_hashes) = next(
        (version, hashes)
        for ((name, version), hashes) in locked_requirements.items()
        if name == "packaging"
    )
    packaging_wheel_name = f"packaging-{packaging_version}-py3-none-any.whl"
    packaging_digest = digest(Path("/wheelhouse") / packaging_wheel_name)
    if packaging_digest not in accepted_hashes:
        raise ValueError("Cannot qualify wheelhouse bundle: cached packaging wheel differs from its lock.")

    selection_lock = scratch / "pylock.toml"
    selection_lock.write_text(
        'lock-version = "1.0"\ncreated-by = "release qualification"\n'
        f'[[packages]]\nname = "packaging"\nversion = "{packaging_version}"\n'
        f'[[packages.wheels]]\nurl = "https://example.invalid/{packaging_wheel_name}"\n'
        f'hashes = {{ sha256 = "{packaging_digest}" }}\n'
    )
    projected_lock = scratch / "pylock.local.toml"
    wheelhouse_command = ["python3", "-I", "-S", str(bundle)]
    subprocess.run(
        [*wheelhouse_command, "locate", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    selected_wheels = subprocess.run(
        [*wheelhouse_command, "select", "--lock", str(selection_lock)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    if json.loads(selected_wheels.stdout)["artifacts"][0]["sha256"] != packaging_digest:
        raise ValueError("Cannot qualify wheelhouse bundle: target selection differs from the lock.")

    subprocess.run(
        [
            *wheelhouse_command,
            "check",
            "--lock",
            str(selection_lock),
            "--pool",
            "/wheelhouse",
            "--local-lock",
            str(projected_lock),
            "--local-root",
            "/wheels",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    projected_artifact = tomllib.loads(projected_lock.read_text())["packages"][0]["wheels"][0]
    projected_hashes = projected_artifact.get("hashes")
    if (
        projected_artifact.get("path") != "/wheels/" + packaging_wheel_name
        or "url" in projected_artifact
        or type(projected_hashes) is not dict
        or projected_hashes.get("sha256") != packaging_digest
    ):
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
            *wheelhouse_command,
            "report",
            "--report",
            str(report),
            "--pool",
            "/wheelhouse",
            "--directory",
            "wheels",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    expected_report = [
        {"filename": packaging_wheel_name, "sha256": packaging_digest, "directory": "wheels"}
    ]
    if json.loads(report_conversion.stdout) != expected_report:
        raise ValueError("Cannot qualify wheelhouse bundle: pip report projection differs from the selected artifact.")

    manifest.write_text(report_conversion.stdout)
    manifest_identity = subprocess.run(
        [*wheelhouse_command, "identity", "--manifest", str(manifest)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    canonical_manifest = json.dumps(
        json.loads(report_conversion.stdout),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
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
            *wheelhouse_command,
            "stream",
            "--pool",
            "/wheelhouse",
            "--source-allowlist",
            str(source_allowlist),
            "--manifest",
            str(manifest),
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(streamed_context)) as archive:
        if set(archive.getnames()) != {"Dockerfile", "wheels/" + packaging_wheel_name}:
            raise ValueError("Cannot qualify wheelhouse bundle: streamed inputs differ from the selected context.")

        with archive.extractfile("wheels/" + packaging_wheel_name) as streamed_wheel:
            if hashlib.file_digest(streamed_wheel, "sha256").hexdigest() != packaging_digest:
                raise ValueError("Cannot qualify wheelhouse bundle: streamed wheel differs from the lock.")



def qualify_acquisition_bundle(bundle, scratch, expected_source_files):
    """Validate the pinned source inventory before extracting or importing code."""
    if type(expected_source_files) is not dict or not expected_source_files:
        raise ValueError("Cannot qualify acquisition source bundle: missing source inventory.")

    with tarfile.open(bundle, "r:gz") as archive:
        verified_sources = {}
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if (
                not member.name
                or not member_path.parts
                or member_path.is_absolute()
                or ".." in member_path.parts
                or member_path.as_posix() != member.name
                or not member.isfile()
                or member.name in verified_sources
            ):
                raise ValueError(
                    "Cannot qualify acquisition source bundle: unsafe or duplicate member."
                )

            expected_member = expected_source_files.get(member.name)
            if (
                type(expected_member) is not dict
                or set(expected_member) != {"sha256", "size", "mode"}
                or member.size != expected_member["size"]
                or member.mode != expected_member["mode"]
            ):
                raise ValueError(
                    "Cannot qualify acquisition source bundle: member differs from source inventory."
                )

            with archive.extractfile(member) as reader:
                member_bytes = reader.read()
            if hashlib.sha256(member_bytes).hexdigest() != expected_member["sha256"]:
                raise ValueError(
                    "Cannot qualify acquisition source bundle: member checksum "
                    "differs from source inventory."
                )
            verified_sources[member.name] = member_bytes

    if set(verified_sources) != set(expected_source_files):
        raise ValueError("Cannot qualify acquisition source bundle: incomplete source inventory.")
    for name in verified_sources:
        if any(parent.as_posix() in verified_sources for parent in PurePosixPath(name).parents):
            raise ValueError(
                "Cannot qualify acquisition source bundle: member conflicts with its parent path."
            )

    destination = scratch / "acquisition-source"
    destination.mkdir()
    for (name, member_bytes) in verified_sources.items():
        extracted_file = destination / name
        extracted_file.parent.mkdir(parents=True, exist_ok=True)
        with extracted_file.open("xb") as output:
            output.write(member_bytes)

    subprocess.run(["bash", "-n", str(destination / "acquisition_tool/owner.sh")], check=True)
    cold_import_check = Path(__file__).with_name("acquisition_import_check.py")
    subprocess.run(
        ["python3", "-I", "-S", "-B", str(cold_import_check), str(destination)],
        check=True,
    )
    for entrypoint in ("identity.py", "proxy.py"):
        subprocess.run(
            [
                "python3",
                "-I",
                "-S",
                "-B",
                str(destination / "acquisition_tool" / entrypoint),
                "--help",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
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
                member.name in member_names
                or not member.name
                or member_path.is_absolute()
                or ".." in member_path.parts
                or member_path.as_posix() != member.name
                or member_path.parts[0] != root_name
                or not (member.isfile() or member.isdir())
                or member.mode & 0o022
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
    if type(inventory) is not dict or not inventory:
        raise ValueError("Cannot qualify package: the pre-build source inventory is missing.")

    for (name, facts) in inventory.items():
        member_path = PurePosixPath(name)
        if (
            member_path.is_absolute()
            or ".." in member_path.parts
            or member_path.as_posix() != name
            or not (
                name in {"README.md", "MANIFEST.in", "pyproject.toml"}
                or name.startswith(("src/provider_core/", "tests/"))
            )
            or type(facts) is not dict
            or set(facts) != {"sha256", "size", "mode"}
        ):
            raise ValueError(
                "Cannot qualify package: the recorded source inventory "
                "has unsupported entries."
            )

        source_file = source / name
        if (
            not source_file.is_file()
            or digest(source_file) != facts["sha256"]
            or source_file.stat().st_size != facts["size"]
            or source_file.stat().st_mode & 0o777 != facts["mode"]
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
        if len(archive_members) != len(set(archive_members)) or any(
            not name.startswith(metadata_prefix) and name not in expected
            for name in archive_members
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
    # The wheel and runtime-install lock remain exactly as issued. Behavioral
    # tests get the source archive's own test lock; contract/native proofs do not.
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
            str(selected_python),
            "-I",
            "-B",
            str(test_root / "tests/run_suite.py"),
            suite,
            "test*.py",
        ]
        test_process = subprocess.run(test_command, cwd=test_root)
        if test_process.returncode:
            raise SystemExit(
                f"Installed provider-core {suite} release qualification failed "
                f"(exit {test_process.returncode}). See the suite diagnostic above "
                "and the retained release-test.log."
            )


def check(artifacts, revision):
    with tempfile.TemporaryDirectory(prefix="package-proof-") as temporary:
        scratch = Path(temporary)
        for path in artifacts.iterdir():
            with regular_file(path):
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
        if {path.name for path in artifacts.iterdir()} != artifact_names(release_version):
            raise ValueError("Cannot qualify package: expect exactly the declared release artifacts.")

        checksum_manifest_path = artifacts / (artifact_prefix + "-sha256.json")
        original_manifest_digest = digest(checksum_manifest_path)
        recorded_hashes = json.loads(checksum_manifest_path.read_text())
        observed_hashes = {
            path.name: digest(path)
            for path in artifacts.iterdir()
            if not path.name.endswith("-sha256.json")
        }
        if recorded_hashes != observed_hashes:
            raise ValueError("Cannot qualify package: captured release hashes do not match the declared artifacts.")

        release_record = json.loads((artifacts / (artifact_prefix + "-release.json")).read_text())
        if (
            release_record["source_revision"] != revision
            or release_record["files"] != {
                name: value
                for (name, value) in observed_hashes.items()
                if not name.endswith("-release.json")
            }
        ):
            raise ValueError(
                "Cannot qualify package: source revision or recorded artifact "
                "identities differ from this release."
            )
        if release_record["tool_source_files"] != wheelhouse_source_inventory(Path("/workspace/wheelhouse_tool")):
            raise ValueError("Cannot qualify package: wheelhouse tool source differs from the release inputs.")

        wheelhouse_bundle = artifacts / f"wheelhouse-tool-{release_version}.pyz"
        rebuilt_wheelhouse_bundle = scratch / wheelhouse_bundle.name
        build_wheelhouse_bundle(
            Path("/wheelhouse"),
            rebuilt_wheelhouse_bundle,
            Path("/workspace"),
            Path("/wheelhouse-tool.lock"),
        )
        if wheelhouse_bundle.read_bytes() != rebuilt_wheelhouse_bundle.read_bytes():
            raise ValueError("Cannot qualify package: wheelhouse bundle differs from its source rebuild.")
        qualify_wheelhouse_bundle(wheelhouse_bundle, scratch)

        acquisition_bundle = artifacts / f"acquisition-owner-{release_version}.tar.gz"
        rebuilt_acquisition_bundle = scratch / acquisition_bundle.name
        acquisition_record = build_acquisition_bundle(
            Path("/wheelhouse"),
            rebuilt_acquisition_bundle,
            Path("/workspace"),
            captured_wheels[0],
            Path("/acquisition-bootstrap.pylock.toml"),
        )
        if (
            acquisition_bundle.read_bytes() != rebuilt_acquisition_bundle.read_bytes()
            or release_record["acquisition_source_files"] != acquisition_record["source_files"]
            or release_record["acquisition_dependency_inputs"] != acquisition_record["dependency_inputs"]
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
            source_project["project"]["name"] != "provider-core"
            or source_project["project"]["version"] != release_version
        ):
            raise ValueError("Cannot qualify package: source project and version differ from the release artifacts.")

        source_python_files = source_payload(extracted_source, release_record["source_files"])
        original_wheel_metadata = wheel_payload(captured_wheels[0], source_python_files, release_version)

        runtime_install_lock = artifacts / (artifact_prefix + "-install.lock")
        if requirements(runtime_install_lock) != requirements(Path("/core-runtime.lock")) | {
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
            Path("/core-runtime.lock"),
            Path("/core-build.lock"),
            Path("/wheelhouse"),
            rebuild_output,
        )
        if (
            rebuilt_dependency_inputs != release_record["dependency_inputs"]
            or wheel_payload(rebuilt_wheel, source_python_files, release_version) != original_wheel_metadata
        ):
            raise ValueError(
                "Cannot qualify package: source rebuild metadata or dependency "
                "inputs differ from the original wheel."
            )

        runtime_root = Path("/runtime/package-proof")
        runtime_root.mkdir()
        qualify_installed(
            captured_wheels[0],
            runtime_install_lock,
            extracted_source,
            Path("/wheelhouse"),
            runtime_root / "original",
            ("unit", "contract", "integration"),
        )
        qualify_installed(
            rebuilt_wheel,
            rebuilt_lock,
            extracted_source,
            Path("/wheelhouse"),
            runtime_root / "rebuilt",
            ("contract",),
        )
        qualify_installed(
            captured_wheels[0],
            native_install_lock,
            extracted_source,
            Path("/wheelhouse"),
            runtime_root / "native",
            ("native",),
        )

        if (
            digest(checksum_manifest_path) != original_manifest_digest
            or {
                path.name: digest(path)
                for path in artifacts.iterdir()
                if not path.name.endswith("-sha256.json")
            } != observed_hashes
        ):
            raise ValueError("Cannot qualify package: release artifacts changed during qualification.")

        for (name, checksum) in sorted(
            (observed_hashes | {checksum_manifest_path.name: original_manifest_digest}).items()
        ):
            print("QUALIFIED_SHA256", checksum, name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    check(args.artifacts, args.revision)
