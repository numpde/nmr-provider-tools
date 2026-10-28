"""Qualify the exact GitHub release artifacts, including their source rebuild.

Follows pay-to-play-wire 4c92a3b tools/ops/package.py. The pre-backend source
inventory is independent of setuptools' file list; tests run against installed
wheels with only source-distribution test files on their test path.
"""

from tools.container_guard import require_container

require_container()

import argparse
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
from tools.core_environment import build_candidate, install_environment
from tools.wheelhouse import capture, digest, requirements


def artifact_names(version):
    prefix = f"provider_core-{version}"
    return {prefix + suffix for suffix in ("-py3-none-any.whl", ".tar.gz", "-install.lock", "-native-install.lock", "-release.json", "-sha256.json")}


def unpack_source(archive, destination, version):
    """Admit the complete tar inventory before creating any extracted file."""
    root_name = f"provider_core-{version}"
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (member.name in names or not member.name or path.is_absolute() or ".." in path.parts
                    or path.as_posix() != member.name or path.parts[0] != root_name
                    or not (member.isfile() or member.isdir()) or member.mode & 0o022):
                raise ValueError("Cannot qualify package: source archive has duplicate, unsafe or unsupported members.")
            names.add(member.name)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                target.chmod(member.mode & 0o777)
    return destination / root_name


def source_payload(source, inventory):
    if type(inventory) is not dict or not inventory:
        raise ValueError("Cannot qualify package: the pre-build source inventory is missing.")
    for name, facts in inventory.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != name
                or not (name in {"README.md", "MANIFEST.in", "pyproject.toml"} or name.startswith(("src/provider_core/", "tests/")))
                or type(facts) is not dict or set(facts) != {"sha256", "size", "mode"}):
            raise ValueError("Cannot qualify package: the recorded source inventory has unsupported entries.")
        actual = source / name
        if (not actual.is_file() or digest(actual) != facts["sha256"] or actual.stat().st_size != facts["size"]
                or actual.stat().st_mode & 0o777 != facts["mode"]):
            raise ValueError("Cannot qualify package: source distribution differs from its pre-build source inventory.")
    generated = {"PKG-INFO", "setup.cfg"} | {
        "src/provider_core.egg-info/" + name
        for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt", "top_level.txt")
    }
    observed = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
    if observed != set(inventory) | generated:
        raise ValueError("Cannot qualify package: source distribution has missing or unrecorded authored files.")
    return {name.removeprefix("src/"): (source / name).read_bytes() for name in inventory if name.startswith("src/provider_core/")}


def wheel_payload(wheel, expected, version):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = f"provider_core-{version}.dist-info/"
        if len(names) != len(set(names)) or any(not name.startswith(metadata) and name not in expected for name in names):
            raise ValueError("Cannot qualify package: wheel contains duplicate or unexpected payload files.")
        payload = {name: archive.read(name) for name in names if not name.startswith(metadata)}
        if payload != expected:
            raise ValueError("Cannot qualify package: installed wheel payload differs from its captured source files.")
        return {name.removeprefix(metadata): archive.read(name) for name in names if name.startswith(metadata) and not name.endswith("/RECORD")}


def qualify_installed(wheel, lock, source, pool, root, suites):
    selected_pool = root / "pool"
    selected_pool.mkdir(parents=True)
    capture(Path("/core-runtime.lock"), pool, selected_pool)
    with regular_file(wheel) as reader, (selected_pool / wheel.name).open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    python = install_environment(root / "runtime", lock, selected_pool)
    test_root = root / "run"
    shutil.copytree(source / "tests", test_root / "tests")
    for suite in suites:
        subprocess.run([str(python), "-I", "-B", str(test_root / "tests/run_suite.py"), suite, "test*.py"], cwd=test_root, check=True)


def check(artifacts, revision):
    with tempfile.TemporaryDirectory(prefix="package-proof-") as temporary:
        scratch = Path(temporary)
        captured = scratch / "artifacts"
        captured.mkdir()
        for path in artifacts.iterdir():
            with regular_file(path) as reader, (captured / path.name).open("xb") as writer:
                shutil.copyfileobj(reader, writer)
        wheels = list(captured.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Cannot qualify package: expect exactly one provider-core wheel.")
        match = re.fullmatch(r"provider_core-((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-py3-none-any.whl", wheels[0].name)
        if match is None:
            raise ValueError("Cannot qualify package: wheel name must identify a provider-core release version.")
        version = match[1]
        prefix = f"provider_core-{version}"
        if {path.name for path in captured.iterdir()} != artifact_names(version):
            raise ValueError("Cannot qualify package: expect exactly the six declared release artifacts.")
        manifest = json.loads((captured / (prefix + "-sha256.json")).read_text())
        expected = {path.name: digest(path) for path in captured.iterdir() if not path.name.endswith("-sha256.json")}
        if manifest != expected:
            raise ValueError("Cannot qualify package: captured release hashes do not match the declared artifacts.")
        record = json.loads((captured / (prefix + "-release.json")).read_text())
        if record["source_revision"] != revision or record["files"] != {name: value for name, value in expected.items() if not name.endswith("-release.json")}:
            raise ValueError("Cannot qualify package: source revision or recorded artifact identities differ from this release.")
        source = unpack_source(captured / (prefix + ".tar.gz"), scratch / "source", version)
        document = tomllib.loads((source / "pyproject.toml").read_text())
        if document["project"]["name"] != "provider-core" or document["project"]["version"] != version:
            raise ValueError("Cannot qualify package: source project and version differ from the release artifacts.")
        payload = source_payload(source, record["source_files"])
        original_metadata = wheel_payload(wheels[0], payload, version)
        lock = captured / (prefix + "-install.lock")
        if requirements(lock) != requirements(Path("/core-runtime.lock")) | {("provider-core", version): {digest(wheels[0])}}:
            raise ValueError("Cannot qualify package: issued install lock differs from the closed runtime dependencies and wheel.")
        native_lock = captured / (prefix + "-native-install.lock")
        if requirements(native_lock) != {("provider-core", version): {digest(wheels[0])}}:
            raise ValueError("Cannot qualify package: native install lock differs from the same released wheel.")
        rebuilt = scratch / "rebuilt"
        rebuilt.mkdir()
        rebuilt_wheel, rebuilt_lock, identities = build_candidate(
            source, Path("/core-runtime.lock"), Path("/core-build.lock"), Path("/wheelhouse"), rebuilt,
        )
        if identities != record["dependency_inputs"] or wheel_payload(rebuilt_wheel, payload, version) != original_metadata:
            raise ValueError("Cannot qualify package: source rebuild metadata or dependency inputs differ from the original wheel.")
        runtime = Path("/runtime/package-proof")
        runtime.mkdir()
        qualify_installed(wheels[0], lock, source, Path("/wheelhouse"), runtime / "original", ("unit", "contract", "integration"))
        qualify_installed(rebuilt_wheel, rebuilt_lock, source, Path("/wheelhouse"), runtime / "rebuilt", ("contract",))
        qualify_installed(wheels[0], native_lock, source, Path("/wheelhouse"), runtime / "native", ("native",))
        for path in sorted(captured.iterdir()):
            print("QUALIFIED_SHA256", digest(path), path.name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    check(args.artifacts, args.revision)
