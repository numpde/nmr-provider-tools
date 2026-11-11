"""Verify or refresh the last qualified acquisition bootstrap in this checkout.

Refresh is explicit source maintenance with acquisitions quiescent. Normal
operations verify the checked-in tree and never extract or download it.
Refresh/rollback follows NMRPeak repository_checks/acquisition_bootstrap.py.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile


def verify(bundle: Path, record: dict) -> None:
    inventory = record['acquisition_source_files']
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError('Acquisition bootstrap release inventory is empty.')
    expected = set()
    expected_directories = set()
    for name in inventory:
        path = PurePosixPath(name)
        if not isinstance(name, str) or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
            raise ValueError('Acquisition bootstrap release inventory contains an invalid path.')
        expected.add(name)
        expected_directories.update(parent.as_posix() for parent in path.parents if parent != PurePosixPath('.'))
    actual = set()
    for (current, directories, files) in os.walk(bundle, followlinks=False):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError('Acquisition bootstrap contains a symlink directory.')
            if (parent / name).relative_to(bundle).as_posix() not in expected_directories:
                raise ValueError('Acquisition bootstrap directories differ from the release inventory.')
        for name in files:
            path = parent / name
            relative = path.relative_to(bundle).as_posix()
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f'Acquisition bootstrap member is not a regular file: {relative}')
            actual.add(relative)
    if bundle.is_symlink() or not bundle.is_dir() or actual != expected:
        raise ValueError('Acquisition bootstrap members differ from the release inventory.')
    for (name, entry) in inventory.items():
        path = bundle / name
        data = path.read_bytes()
        if (len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']
                or (stat.S_IMODE(path.stat().st_mode) & 0o111) != (entry['mode'] & 0o111)):
            raise ValueError(f'Acquisition bootstrap member differs from the release record: {name}')


def refresh(bundle: Path, record: dict, archive_path: Path, version: str,
            record_output: Path, record_bytes: bytes) -> None:
    name = f'acquisition-owner-{version}.tar.gz'
    data = archive_path.read_bytes()
    if archive_path.name != name or hashlib.sha256(data).hexdigest() != record['files'][name]:
        raise ValueError('Acquisition bootstrap archive differs from the release record.')
    inventory = record['acquisition_source_files']
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) != len(inventory) or {m.name for m in members} != set(inventory):
            raise ValueError('Acquisition bootstrap archive members differ from the release inventory.')
        contents = {}
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or path.as_posix() != member.name or not member.isfile():
                raise ValueError('Acquisition bootstrap archive contains an unsupported member.')
            stream = archive.extractfile(member)
            assert stream is not None
            content = stream.read()
            entry = inventory[member.name]
            if len(content) != entry['size'] or hashlib.sha256(content).hexdigest() != entry['sha256'] or member.mode != entry['mode']:
                raise ValueError(f'Acquisition bootstrap archive member differs from the release record: {member.name}')
            contents[member.name] = content
    if bundle.parent.is_symlink():
        raise ValueError('Acquisition bootstrap parent must not be a symlink.')
    if not bundle.parent.exists():
        bundle.parent.mkdir(parents=True)
        bundle.parent.chmod(0o755)
    if not bundle.parent.is_dir():
        raise ValueError('Acquisition bootstrap parent must be a directory.')
    if bundle.is_symlink() or (bundle.exists() and not bundle.is_dir()):
        raise ValueError('Acquisition bootstrap destination must be a non-symlink directory.')
    if bundle.exists():
        verify(bundle, json.loads(record_output.read_bytes()))
    stage = Path(tempfile.mkdtemp(prefix='.acquisition-', dir=bundle.parent))
    backup = None
    staged_record = None
    try:
        with tempfile.NamedTemporaryFile(dir=record_output.parent, prefix='.acquisition-record-', delete=False) as output:
            staged_record = Path(output.name)
            output.write(record_bytes)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        for (name, content) in contents.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(inventory[name]['mode'])
        # The published source is public and must be traversable by checker
        # users even when refresh runs with a private operator umask.
        stage.chmod(0o755)
        for directory in stage.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)
        verify(stage, record)
        if bundle.exists():
            backup = Path(tempfile.mkdtemp(prefix='.acquisition-previous-', dir=bundle.parent))
            backup.rmdir()
            bundle.rename(backup)
        try:
            stage.rename(bundle)
            os.replace(staged_record, record_output)
        except BaseException:
            if not stage.exists() and bundle.exists():
                bundle.rename(stage)
            if backup is not None:
                backup.rename(bundle)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if staged_record is not None:
            staged_record.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("issuer", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--record-output", type=Path)
    args = parser.parse_args()
    record_bytes = args.issuer.read_bytes()
    record = json.loads(record_bytes)
    if args.archive is None:
        verify(args.source, record)
        return
    names = [name for name in record["files"] if name.startswith("acquisition-owner-") and name.endswith(".tar.gz")]
    if len(names) != 1:
        raise ValueError("Acquisition issuer must select one exact source archive.")
    version = names[0].removeprefix("acquisition-owner-").removesuffix(".tar.gz")
    if args.record_output is None:
        raise ValueError("Refresh requires the checked-in issuer record output.")
    refresh(args.source, record, args.archive, version, args.record_output, record_bytes)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, EOFError, tarfile.TarError) as error:
        raise SystemExit(f"Cannot prepare acquisition bootstrap: {error}") from None
