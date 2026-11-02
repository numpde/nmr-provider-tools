"""Build the shared wheelhouse zipapp from its pinned pure-Python wheels."""

import argparse
import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path


DEPENDENCIES = {
    "packaging-26.3-py3-none-any.whl": "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
    "tomli_w-1.2.0-py3-none-any.whl": "188306098d013b691fcadc011abd66727d3c414c571bb01b1a174ba8c983cf90",
}


def build(pool, output, source):
    files = {"__main__.py": b"import sys\nif sys.argv[1:2] == ['stream']:\n    del sys.argv[1]\n    from wheelhouse_tool.stream import main\nelse:\n    from wheelhouse_tool.__main__ import main\nraise SystemExit(main())\n"}
    for name in ("__init__.py", "__main__.py", "stream.py"):
        files[f"wheelhouse_tool/{name}"] = (source / "wheelhouse_tool" / name).read_bytes()
    for name, expected in DEPENDENCIES.items():
        wheel = pool / name
        data = wheel.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Cannot build wheelhouse bundle: {name} differs from its pinned hash.")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    if member.filename in files:
                        raise ValueError(f"Cannot build wheelhouse bundle: duplicate {member.filename}.")
                    files[member.filename] = archive.read(member)
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
    args = parser.parse_args()
    build(args.pool, args.output, Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()
