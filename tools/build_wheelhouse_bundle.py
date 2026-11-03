"""Build the shared wheelhouse zipapp from its pinned pure-Python wheels."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path

from tools.wheelhouse import requirements

def dependency_files(pool, lock):
    files = {}
    for (package, version), hashes in requirements(lock).items():
        name = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
        wheel = pool / name
        data = wheel.read_bytes()
        if hashlib.sha256(data).hexdigest() not in hashes:
            raise ValueError(f"Cannot build wheelhouse bundle: {name} differs from its pinned hash.")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    if member.filename in files:
                        raise ValueError(f"Cannot build wheelhouse bundle: duplicate {member.filename}.")
                    files[member.filename] = archive.read(member)
    return files


def build(pool, output, source, lock):
    files = dependency_files(pool, lock)
    files["__main__.py"] = b"import sys\nif sys.argv[1:2] == ['stream']:\n    del sys.argv[1]\n    from wheelhouse_tool.stream import main\nelif sys.argv[1:2] == ['report']:\n    del sys.argv[1]\n    from wheelhouse_tool.report import main\nelif sys.argv[1:2] == ['identity']:\n    del sys.argv[1]\n    from wheelhouse_tool.identity import main\nelif sys.argv[1:2] == ['locate']:\n    del sys.argv[1]\n    from wheelhouse_tool.locate import main\nelse:\n    from wheelhouse_tool.__main__ import main\nraise SystemExit(main())\n"
    for name in ("__init__.py", "__main__.py", "stream.py", "report.py", "identity.py", "locate.py", "network.py"):
        files[f"wheelhouse_tool/{name}"] = (source / "wheelhouse_tool" / name).read_bytes()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".part", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, content in sorted(files.items()):
                    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_STORED
                    entry.external_attr = 0o644 << 16
                    archive.writestr(entry, content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args()
    build(args.pool, args.output, Path(__file__).resolve().parent.parent, args.lock)


if __name__ == "__main__":
    main()
