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
import tomllib
from urllib.parse import urlsplit
import zipfile

from tools.artifacts import fsync_directory, regular_file
from tools.adapters import pylock
from tools.installer import release as installer_release, wheel_bytes


# These are the authored entrypoints and existing canonical shell owners.
# repo-check: allow-module-constant
SOURCE_FILES = (
    "acquisition_tool/owner.sh",
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


def dependency_files(pool, pin):
    (package,) = pylock.packages(tomllib.loads(pin.read_text()))
    (wheel,) = pylock.wheels(package)

    package_name = pylock.package_name(package)
    expected_package_name = "linux-idmap"

    if package_name != expected_package_name:
        raise ValueError(f"{reprlib.repr(package_name)=} should match {expected_package_name=}")

    wheel_name = PurePosixPath(urlsplit(pylock.artifact_url(wheel)).path).name
    expected_wheel_name = f"linux_idmap-{pylock.package_version(package)}-py3-none-any.whl"  # machine-string: qualified wheel filename

    if wheel_name != expected_wheel_name:
        raise ValueError(f"{reprlib.repr(wheel_name)=} should match {reprlib.repr(expected_wheel_name)=}")

    wheel_bytes = (pool / wheel_name).read_bytes()
    wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
    expected_sha256 = pylock.artifact_sha256(wheel)

    if not (wheel_sha256 == expected_sha256):
        raise ValueError(f"For {wheel_name=}, {wheel_sha256=} should match {expected_sha256=}")

    files = {}

    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        for member in archive.infolist():
            if not member.is_dir():
                _add(files, member.filename, archive.read(member))

    return (files, {wheel_name: wheel_sha256})


def contents(pool, source, core_wheel, pin, *, revision, wheelhouse_bundle):
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Cannot build acquisition manifest: require the captured Git source revision.")

    version = core_wheel.name.removeprefix("provider_core-").removesuffix("-py3-none-any.whl")

    if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None:
        raise ValueError("Cannot build acquisition manifest: the SDK wheel must identify an exact release version.")

    (files, dependencies) = dependency_files(pool, pin)

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

    # Select the same built tool artifact that the SDK issuer records.
    # Its version is not inferred from the owner version or an older consumer pin.
    if re.fullmatch(r"wheelhouse-tool-[0-9]+\.[0-9]+\.[0-9]+\.pyz", wheelhouse_bundle.name) is None:
        raise ValueError("Cannot build acquisition manifest: require one built wheelhouse tool artifact.")

    with regular_file(wheelhouse_bundle) as stream:
        dependencies[wheelhouse_bundle.name] = hashlib.file_digest(stream, "sha256").hexdigest()

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

            if member_names.count(member_name) != 1:
                raise ValueError(
                    "Cannot build acquisition source bundle: SDK proxy member "
                    "is missing or duplicated."
                )

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

    return (files, {"source_files": source_inventory, "dependency_inputs": dependencies})


def build(pool, output, source, core_wheel, pin, *, revision, wheelhouse_bundle):
    (files, source_record) = contents(
        pool,
        source,
        core_wheel,
        pin,
        revision=revision,
        wheelhouse_bundle=wheelhouse_bundle,
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
