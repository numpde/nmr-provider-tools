"""Build the cold acquisition source closure from committed and pinned bytes."""

from tools.container_guard import require_container

require_container()

import ast
import gzip
import hashlib
import io
import json
import os
import re
import reprlib
import pathlib
from pathlib import PurePosixPath
import tarfile
import tempfile
import subprocess
import sys
import zipfile

from tools.artifacts import fsync_directory, regular_file
from tools.wheelhouse import digest, read_json
from tools.installer import release as installer_release, wheel_bytes


# These are the authored entrypoints and existing canonical shell owners.
# repo-check: allow-module-constant
SOURCE_FILES = (
    "acquisition_tool/owner.sh",
    "acquisition_tool/gateway.py",
    "acquisition_tool/compose.sh",
    "acquisition_tool/source_update.py",
    "acquisition_tool/source_download.py",
    "acquisition_tool/maintain_source.sh",
    "acquisition_tool/adapters/__init__.py",
    "acquisition_tool/adapters/docker_container.sh",
    "acquisition_tool/adapters/host_resolver.py",
    "acquisition_tool/adapters/public_release.py",
    "acquisition_tool/__init__.py",
    "acquisition_tool/identity.py",
    "acquisition_tool/identity_main.py",
    "acquisition_tool/dns.py",
    "acquisition_tool/context.py",
    "acquisition_tool/validation/upstream/container_ids.sh",
    "acquisition_tool/validation/upstream/docker_container.sh",
    "acquisition_tool/validation/upstream/host_resolver.py",
    "acquisition_tool/validation/upstream/linux_idmap.py",
    "tools/inference_native.sh",
    "tools/docker_client.sh",
)

# The proxy and its logging formatter use the SDK's canonical sources.
# repo-check: allow-module-constant
PROXY_FILES = (
    "__init__.py",
    "container_guard.py",
    "bound_http_proxy.py",
    "diagnostic_text.py",
    "adapters/__init__.py",
    "adapters/dns.py",
    "adapters/http_proxy.py",
    "validation/__init__.py",
    "validation/upstream/__init__.py",
    "validation/upstream/dns.py",
    "validation/upstream/host_resolver.py",
    "validation/upstream/http_proxy.py",
)

# Consumers bake this qualified helper package and its inputs into tool images.
# repo-check: allow-module-constant
INSTALLER_SOURCES = (
    ("packages/type-requirements/src/type_requirements.py", "type_requirements.py"),
    ("tools/installer.py", "acquisition_tool/installer/tools/installer.py"),
    ("tools/container_guard.py", "acquisition_tool/installer/tools/container_guard.py"),
    ("tools/adapters/__init__.py", "acquisition_tool/installer/tools/adapters/__init__.py"),
    ("tools/adapters/pip_install.py", "acquisition_tool/installer/tools/adapters/pip_install.py"),
    ("requirements/installer-release.json", "acquisition_tool/installer/requirements/installer-release.json"),
)


def _add(files, member_name, member_bytes):
    member_path = PurePosixPath(member_name)

    if not (
        (not member_path.is_absolute()) and
        (".." not in member_path.parts) and
        (member_path.as_posix() == member_name)
    ):
        raise ValueError(f"{member_name=} should be a canonical relative path within the acquisition archive")

    if not (member_name not in files):
        raise ValueError(f"{member_name=} should appear only once in the acquisition archive")

    files[member_name] = member_bytes


def dependency_files(pool, pin, tool):
    selection = subprocess.run(
        [
            sys.executable,  # Match the bootstrap lock's target interpreter.
            "-I",  # Ignore user Python paths and environment settings.
            "-B",  # Keep bytecode out of captured source directories.
            str(tool),  # Execute the independently verified tool bundle.
            "check",  # Verify the complete bootstrap selection without networking.
            "--lock",  # Read the captured environment lock.
            str(pin),
            "--pool",  # Read the caller's shared wheel directory.
            str(pool),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )

    (files, dependencies) = ({}, {})

    for wheel in json.loads(selection.stdout)["artifact_entries"]:
        wheel_name = wheel["filename"]

        # Selection has been verified; authenticate the bytes captured for this
        # archive as well, so a subsequent pool mutation cannot enter its payload.
        with regular_file(pathlib.Path(wheel["source_path"])) as stream:
            wheel_bytes = stream.read()

        wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()

        if not (wheel_sha256 == wheel["sha256"]):
            raise ValueError(
                f"Cannot bundle {wheel_name=!s}: captured SHA-256 {wheel_sha256=!s} "
                f"differs from checked acquisition SHA-256 {wheel['sha256']=!s}"
            )

        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    _add(files, member.filename, archive.read(member))

        dependencies[wheel_name] = wheel_sha256

    return (files, dependencies)


def wheelhouse_release(pool, pin):
    """Authenticate the independently published tool used by SDK workflows."""

    record = read_json(pin)
    bundle = pool / record["name"]
    observed_sha256 = digest(bundle)

    if not (observed_sha256 == record["sha256"]):
        raise ValueError(
            f"Cannot use Wheelhouse dependency {bundle=!s}: SHA-256 {observed_sha256=!s} "
            f"differs from {record['sha256']=!s} in {pin=!s}"
        )

    return (record, bundle)


def contents(pool, source, core_wheel, pin, *, revision):
    expected_revision_pattern = r"[0-9a-f]{40}"

    if not re.fullmatch(expected_revision_pattern, revision):
        raise ValueError(f"{reprlib.repr(revision)=} should match {expected_revision_pattern=}")

    version = core_wheel.name.removeprefix("provider_core-").removesuffix("-py3-none-any.whl")

    expected_version_pattern = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"

    if not re.fullmatch(expected_version_pattern, version):
        raise ValueError(f"{reprlib.repr(version)=} should match {expected_version_pattern=} for {reprlib.repr(core_wheel.name)=}")

    (tool_release, tool) = wheelhouse_release(pool, source / "requirements/wheelhouse-tool-release.json")
    (files, dependencies) = dependency_files(pool, pin, tool)

    for name in SOURCE_FILES:
        source_path = source / "host" / name

        if name == "acquisition_tool/validation/upstream/container_ids.sh":
            source_path = source / "operations/docker/validation/upstream/container_ids.sh"
        elif name.startswith("acquisition_tool/"):
            source_path = source / "packages/acquisition" / name
        elif name == "tools/docker_client.sh":
            source_path = source / "operations/docker/docker_client.sh"

        _add(files, name, source_path.read_bytes())

    _add(files, "compose/acquisition.yml", (source / "packages/acquisition/compose/acquisition.yml").read_bytes())

    for (source_name, issued_name) in INSTALLER_SOURCES:
        _add(files, issued_name, (source / source_name).read_bytes())

    installer = installer_release(source / "requirements/installer-release.json")

    _add(
        files,
        "acquisition_tool/installer/wheels/" + installer["name"],
        wheel_bytes(pool / installer["name"], installer)
    )

    dependencies[installer["name"]] = installer["sha256"]

    dependencies[tool_release["name"]] = tool_release["sha256"]

    _add(
        files,
        "acquisition_tool/requirements/wheelhouse-release.json",
        (json.dumps(tool_release, sort_keys=True, indent=2) + "\n").encode(),
    )

    with zipfile.ZipFile(core_wheel) as archive:
        member_names = archive.namelist()
        proxy_files = list(PROXY_FILES)

        # Older pinned SDKs lack the helpers; newer ones must supply the complete pair
        helper_files = ("type_requirements.py", "_type_requirements.py")

        proxy_member = "provider_core/bound_http_proxy.py"
        proxy_needs_helpers = False

        if proxy_member in member_names:
            proxy_needs_helpers = any(
                (
                    (isinstance(node, ast.ImportFrom) and
                     ((node.level, node.module) in {(0, "provider_core.type_requirements"), (1, "type_requirements")}))
                    for node in ast.walk(ast.parse(archive.read(proxy_member), filename=proxy_member))
                ),
            )

        if proxy_needs_helpers or any((("provider_core/" + leaf) in member_names) for leaf in helper_files):
            proxy_files.extend(helper_files)

        for leaf in proxy_files:
            member_name = "provider_core/" + leaf

            member_count = member_names.count(member_name)

            if not (member_count == 1):
                raise ValueError(f"{member_count=} should be one for {member_name=} in {reprlib.repr(core_wheel)=}")

            _add(files, member_name, archive.read(member_name))

    payload_inventory = {
        name: {
            "sha256": hashlib.sha256(member_bytes).hexdigest(),
            "size": len(member_bytes),
            "mode": 0o644,
        }
        for (name, member_bytes) in sorted(files.items())
    }

    manifest = (json.dumps(
        {
            "release_version": version,
            "source_revision": revision,
            "payload_files": payload_inventory,
            "dependency_inputs": dependencies,
        },
        sort_keys=True,
        indent=2
    ) + "\n").encode()

    _add(files, "acquisition-manifest.json", manifest)

    # Legacy importers still need every member in the external SDK issuer.
    # Only the embedded inventory excludes itself; the archive digest covers it.
    source_inventory = dict(payload_inventory)

    source_inventory["acquisition-manifest.json"] = {
        "sha256": hashlib.sha256(manifest).hexdigest(),
        "size": len(manifest),
        "mode": 0o644,
    }

    return (files, {"source_files": source_inventory, "dependency_inputs": dependencies, "wheelhouse_release": tool_release})


def build(pool, output, source, core_wheel, pin, *, revision):
    (files, source_record) = contents(
        pool,
        source,
        core_wheel,
        pin,
        revision=revision,
    )

    with tempfile.NamedTemporaryFile(
        prefix="." + output.name + ".",
        suffix=".part",
        dir=output.parent,
    ) as temporary:
        with gzip.GzipFile(fileobj=temporary, mode="wb", filename="", mtime=315532800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for (name, member_bytes) in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.size = len(member_bytes)
                    member.mode = 0o644
                    member.mtime = 315532800
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""

                    archive.addfile(member, io.BytesIO(member_bytes))

        temporary.flush()
        os.fchmod(temporary.fileno(), 0o644)
        os.fsync(temporary.fileno())
        os.link(temporary.name, output)
        fsync_directory(output.parent)

    return source_record
