"""Build the cold acquisition source closure from committed and pinned bytes."""

from tools.container_guard import require_container

require_container()

import gzip
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import tomllib
from urllib.parse import urlsplit
import zipfile

from tools.artifacts import fsync_directory
from tools.installer import release as installer_release, wheel_bytes


# These are the authored entrypoints and existing canonical shell owners.
# repo-check: allow-module-constant
SOURCE_FILES = (
    "acquisition_tool/owner.sh",
    "acquisition_tool/compose.sh",
    "acquisition_tool/__init__.py",
    "acquisition_tool/identity.py",
    "acquisition_tool/identity_main.py",
    "acquisition_tool/dns.py",
    "tools/inference_native.sh",
    "tools/docker_client.sh",
)
# Proxy bootstrap is deliberately the same three members as the SDK wheel.
# repo-check: allow-module-constant
PROXY_FILES = ("__init__.py", "container_guard.py", "bound_http_proxy.py")

# Consumers bake this qualified helper package and its inputs into tool images.
# repo-check: allow-module-constant
INSTALLER_SOURCES = (
    ("tools/installer.py", "acquisition_tool/installer/tools/installer.py"),
    ("tools/container_guard.py", "acquisition_tool/installer/tools/container_guard.py"),
    ("requirements/installer-release.json", "acquisition_tool/installer/requirements/installer-release.json"),
)


def _add(files, name, data):
    member_path = PurePosixPath(name)
    if (
        member_path.is_absolute()
        or ".." in member_path.parts
        or member_path.as_posix() != name
        or name in files
    ):
        raise ValueError(
            "Cannot build acquisition source bundle: duplicate or unsupported member path."
        )
    files[name] = data


def dependency_files(pool, pin):
    (package,) = tomllib.loads(pin.read_text())["packages"]
    (wheel,) = package["wheels"]
    if package["name"] != "linux-idmap":
        raise ValueError(
            "Cannot build acquisition source bundle: require the qualified linux-idmap pin."
        )

    wheel_name = PurePosixPath(urlsplit(wheel["url"]).path).name
    if wheel_name != f"linux_idmap-{package['version']}-py3-none-any.whl":
        raise ValueError(
            "Cannot build acquisition source bundle: the identity dependency "
            "is not its qualified pure-Python wheel."
        )

    wheel_bytes = (pool / wheel_name).read_bytes()
    observed_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
    if observed_sha256 != wheel["hashes"]["sha256"]:
        raise ValueError(
            "Cannot build acquisition source bundle: identity wheel differs from its pinned hash."
        )

    files = {}
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        for member in archive.infolist():
            if not member.is_dir():
                _add(files, member.filename, archive.read(member))
    return files, {wheel_name: observed_sha256}


def contents(pool, source, core_wheel, pin):
    (files, dependencies) = dependency_files(pool, pin)
    for name in SOURCE_FILES:
        _add(files, name, (source / "host" / name).read_bytes())
    _add(files, "compose/acquisition.yml", (source / "compose/acquisition.yml").read_bytes())
    for source_name, issued_name in INSTALLER_SOURCES:
        _add(files, issued_name, (source / source_name).read_bytes())
    installer = installer_release(source / "requirements/installer-release.json")
    _add(files, "acquisition_tool/installer/wheels/" + installer["name"],
         wheel_bytes(pool / installer["name"], installer))
    dependencies[installer["name"]] = installer["sha256"]

    with zipfile.ZipFile(core_wheel) as archive:
        member_names = archive.namelist()
        for leaf in PROXY_FILES:
            member_name = "provider_core/" + leaf
            if member_names.count(member_name) != 1:
                raise ValueError(
                    "Cannot build acquisition source bundle: SDK proxy member "
                    "is missing or duplicated."
                )
            _add(files, member_name, archive.read(member_name))

    source_inventory = {
        name: {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": 0o644,
        }
        for (name, data) in sorted(files.items())
    }
    return files, {"source_files": source_inventory, "dependency_inputs": dependencies}


def build(pool, output, source, core_wheel, pin):
    (files, source_record) = contents(pool, source, core_wheel, pin)
    with tempfile.NamedTemporaryFile(
        prefix="." + output.name + ".",
        suffix=".part",
        dir=output.parent,
    ) as temporary:
        with gzip.GzipFile(fileobj=temporary, mode="wb", filename="", mtime=315532800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for (name, data) in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o644
                    member.mtime = 315532800
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    archive.addfile(member, io.BytesIO(data))

        temporary.flush()
        os.fchmod(temporary.fileno(), 0o644)
        os.fsync(temporary.fileno())
        os.link(temporary.name, output)
        fsync_directory(output.parent)
    return source_record
