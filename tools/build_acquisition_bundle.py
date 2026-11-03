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


# These are the authored entrypoints and existing canonical shell owners.
# repo-check: allow-module-constant
SOURCE_FILES = (
    "acquisition_tool/owner.sh", "acquisition_tool/__init__.py", "acquisition_tool/identity.py", "acquisition_tool/proxy.py",
    "tools/inference_native.sh", "tools/docker_client.sh",
)
# Proxy bootstrap is deliberately the same three members as the SDK wheel.
# repo-check: allow-module-constant
PROXY_FILES = ("__init__.py", "container_guard.py", "bound_http_proxy.py")


def _add(files, name, data):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name or name in files:
        raise ValueError("Cannot build acquisition source bundle: duplicate or unsupported member path.")
    files[name] = data


def dependency_files(pool, pin):
    package, = tomllib.loads(pin.read_text())["packages"]
    wheel, = package["wheels"]
    if package["name"] != "linux-idmap":
        raise ValueError("Cannot build acquisition source bundle: require the qualified linux-idmap pin.")
    name = PurePosixPath(urlsplit(wheel["url"]).path).name
    if name != f"linux_idmap-{package['version']}-py3-none-any.whl":
        raise ValueError("Cannot build acquisition source bundle: the identity dependency is not its qualified pure-Python wheel.")
    data = (pool / name).read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != wheel["hashes"]["sha256"]:
        raise ValueError("Cannot build acquisition source bundle: identity wheel differs from its pinned hash.")
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if not member.is_dir():
                _add(files, member.filename, archive.read(member))
    return files, {name: checksum}


def contents(pool, source, core_wheel, pin):
    files, dependencies = dependency_files(pool, pin)
    for name in SOURCE_FILES:
        _add(files, name, (source / name).read_bytes())
    with zipfile.ZipFile(core_wheel) as archive:
        names = archive.namelist()
        for leaf in PROXY_FILES:
            name = "provider_core/" + leaf
            if names.count(name) != 1:
                raise ValueError("Cannot build acquisition source bundle: SDK proxy member is missing or duplicated.")
            _add(files, name, archive.read(name))
    inventory = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644}
                 for name, data in sorted(files.items())}
    return files, {"source_files": inventory, "dependency_inputs": dependencies}


def build(pool, output, source, core_wheel, pin):
    files, record = contents(pool, source, core_wheel, pin)
    with tempfile.NamedTemporaryFile(prefix="." + output.name + ".", suffix=".part", dir=output.parent) as temporary:
        with gzip.GzipFile(fileobj=temporary, mode="wb", filename="", mtime=315532800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, data in sorted(files.items()):
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
    return record
