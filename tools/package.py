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
    pins = requirements(Path("/wheelhouse-tool.lock"))
    version, hashes = next((version, hashes) for (name, version), hashes in pins.items() if name == "packaging")
    name = f"packaging-{version}-py3-none-any.whl"
    checksum = digest(Path("/wheelhouse") / name)
    if checksum not in hashes:
        raise ValueError("Cannot qualify wheelhouse bundle: cached packaging wheel differs from its lock.")
    lock = scratch / "pylock.toml"
    lock.write_text('lock-version = "1.0"\ncreated-by = "release qualification"\n'
                    f'[[packages]]\nname = "packaging"\nversion = "{version}"\n'
                    f'[[packages.wheels]]\nurl = "https://example.invalid/{name}"\n'
                    f'hashes = {{ sha256 = "{checksum}" }}\n')
    projected = scratch / "pylock.local.toml"
    command = ["python3", "-I", "-S", str(bundle)]
    subprocess.run([*command, "locate", "--help"], check=True, stdout=subprocess.DEVNULL)
    selected = subprocess.run([*command, "select", "--lock", str(lock)], check=True,
                              stdout=subprocess.PIPE, text=True)
    if json.loads(selected.stdout)["artifacts"][0]["sha256"] != checksum:
        raise ValueError("Cannot qualify wheelhouse bundle: target selection differs from the lock.")
    subprocess.run([*command, "check", "--lock", str(lock), "--pool", "/wheelhouse",
                    "--local-lock", str(projected), "--local-root", "/wheels"], check=True, stdout=subprocess.DEVNULL)
    projected_artifact = tomllib.loads(projected.read_text())["packages"][0]["wheels"][0]
    hashes = projected_artifact.get("hashes")
    if (projected_artifact.get("path") != "/wheels/" + name or "url" in projected_artifact
            or type(hashes) is not dict or hashes.get("sha256") != checksum):
        raise ValueError("Cannot qualify wheelhouse bundle: projected artifact differs from the selected lock.")
    dockerfile = scratch / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    manifest = scratch / "stream-manifest.json"
    report = scratch / "pip-report.json"
    report.write_text(json.dumps({"version": "1", "install": [{"download_info": {
        "url": "file:///wheelhouse/" + name, "archive_info": {"hashes": {"sha256": checksum}}}}]}))
    converted = subprocess.run([*command, "report", "--report", str(report), "--pool", "/wheelhouse",
                                "--directory", "wheels"], check=True, stdout=subprocess.PIPE, text=True)
    if json.loads(converted.stdout) != [{"filename": name, "sha256": checksum, "directory": "wheels"}]:
        raise ValueError("Cannot qualify wheelhouse bundle: pip report projection differs from the selected artifact.")
    manifest.write_text(converted.stdout)
    identity = subprocess.run([*command, "identity", "--manifest", str(manifest)], check=True,
                              stdout=subprocess.PIPE, text=True).stdout.strip()
    canonical = json.dumps(json.loads(converted.stdout), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if identity != hashlib.sha256(canonical).hexdigest():
        raise ValueError("Cannot qualify wheelhouse bundle: selection identity differs from the selected artifact.")
    sources = scratch / "stream-sources.json"
    sources.write_text(json.dumps([{"path": str(dockerfile), "destination": "Dockerfile",
                                   "sha256": digest(dockerfile), "executable": False}]))
    output = subprocess.run([*command, "stream", "--pool", "/wheelhouse", "--source-allowlist", str(sources),
                             "--manifest", str(manifest)], check=True, stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(output)) as archive:
        if set(archive.getnames()) != {"Dockerfile", "wheels/" + name}:
            raise ValueError("Cannot qualify wheelhouse bundle: streamed inputs differ from the selected context.")
        with archive.extractfile("wheels/" + name) as selected:
            if hashlib.file_digest(selected, "sha256").hexdigest() != checksum:
                raise ValueError("Cannot qualify wheelhouse bundle: streamed wheel differs from the lock.")


def qualify_acquisition_bundle(bundle, scratch):
    """Exercise cold source imports after exact pinned-source rebuild equality."""
    destination = scratch / "acquisition-source"
    destination.mkdir()
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive.getmembers():
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
    subprocess.run(["bash", "-n", str(destination / "acquisition_tool/owner.sh")], check=True)
    subprocess.run(["python3", "-I", "-S", "-B", "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import linux_idmap.probe; import provider_core.bound_http_proxy",
                    str(destination)], check=True)
    for entrypoint in ("identity.py", "proxy.py"):
        subprocess.run(["python3", "-I", "-S", "-B",
                        str(destination / "acquisition_tool" / entrypoint), "--help"],
                       check=True, stdout=subprocess.DEVNULL)


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
    # The wheel and runtime-install lock remain exactly as issued. Behavioral
    # tests get the source archive's own test lock; contract/native proofs do not.
    python = install_environment(root / "runtime", lock, pool, extra_pools=(wheel.parent,),
                                 required_files={wheel.name: wheel})
    behavioral_python = None
    if any(suite in ("unit", "integration") for suite in suites):
        behavioral_lock = behavioral_install_lock(
            lock, source / "tests/requirements.lock", root / "behavioral-install.lock",
        )
        behavioral_python = install_environment(
            root / "behavioral", behavioral_lock, pool, extra_pools=(wheel.parent,),
            required_files={wheel.name: wheel},
        )
    test_root = root / "run"
    shutil.copytree(source / "tests", test_root / "tests")
    for suite in suites:
        selected_python = behavioral_python if suite in ("unit", "integration") else python
        command = [str(selected_python), "-I", "-B", str(test_root / "tests/run_suite.py"), suite, "test*.py"]
        outcome = subprocess.run(command, cwd=test_root)
        if outcome.returncode:
            raise SystemExit(
                f"Installed provider-core {suite} release qualification failed (exit {outcome.returncode}). "
                "See the suite diagnostic above and the retained release-test.log."
            )


def check(artifacts, revision):
    with tempfile.TemporaryDirectory(prefix="package-proof-") as temporary:
        scratch = Path(temporary)
        captured = artifacts
        for path in artifacts.iterdir():
            with regular_file(path):
                pass
        wheels = list(captured.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Cannot qualify package: expect exactly one provider-core wheel.")
        match = re.fullmatch(r"provider_core-((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-py3-none-any.whl", wheels[0].name)
        if match is None:
            raise ValueError("Cannot qualify package: wheel name must identify a provider-core release version.")
        version = match[1]
        prefix = f"provider_core-{version}"
        if {path.name for path in captured.iterdir()} != artifact_names(version):
            raise ValueError("Cannot qualify package: expect exactly the declared release artifacts.")
        manifest_path = captured / (prefix + "-sha256.json")
        manifest_digest = digest(manifest_path)
        manifest = json.loads(manifest_path.read_text())
        expected = {path.name: digest(path) for path in captured.iterdir() if not path.name.endswith("-sha256.json")}
        if manifest != expected:
            raise ValueError("Cannot qualify package: captured release hashes do not match the declared artifacts.")
        record = json.loads((captured / (prefix + "-release.json")).read_text())
        if record["source_revision"] != revision or record["files"] != {name: value for name, value in expected.items() if not name.endswith("-release.json")}:
            raise ValueError("Cannot qualify package: source revision or recorded artifact identities differ from this release.")
        if record["tool_source_files"] != wheelhouse_source_inventory(Path("/workspace/wheelhouse_tool")):
            raise ValueError("Cannot qualify package: wheelhouse tool source differs from the release inputs.")
        bundle = captured / f"wheelhouse-tool-{version}.pyz"
        rebuilt_bundle = scratch / bundle.name
        build_wheelhouse_bundle(Path("/wheelhouse"), rebuilt_bundle, Path("/workspace"), Path("/wheelhouse-tool.lock"))
        if bundle.read_bytes() != rebuilt_bundle.read_bytes():
            raise ValueError("Cannot qualify package: wheelhouse bundle differs from its source rebuild.")
        qualify_wheelhouse_bundle(bundle, scratch)
        acquisition = captured / f"acquisition-owner-{version}.tar.gz"
        rebuilt_acquisition = scratch / acquisition.name
        acquisition_record = build_acquisition_bundle(
            Path("/wheelhouse"), rebuilt_acquisition, Path("/workspace"), wheels[0],
            Path("/acquisition-bootstrap.pylock.toml"),
        )
        if (acquisition.read_bytes() != rebuilt_acquisition.read_bytes()
                or record["acquisition_source_files"] != acquisition_record["source_files"]
                or record["acquisition_dependency_inputs"] != acquisition_record["dependency_inputs"]):
            raise ValueError("Cannot qualify acquisition source bundle: bytes or provenance differ from the pinned rebuild.")
        qualify_acquisition_bundle(acquisition, scratch)
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
        if (digest(manifest_path) != manifest_digest or
                {path.name: digest(path) for path in captured.iterdir() if not path.name.endswith("-sha256.json")} != expected):
            raise ValueError("Cannot qualify package: release artifacts changed during qualification.")
        for name, checksum in sorted((expected | {manifest_path.name: manifest_digest}).items()):
            print("QUALIFIED_SHA256", checksum, name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    check(args.artifacts, args.revision)
