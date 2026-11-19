"""Encode the pylock archive records consumed by the delivery downloader."""

from tools.container_guard import require_container

require_container()

import json
from collections.abc import Iterable
from typing import TextIO


def write_wheel_archive_lock(
    stream: TextIO,
    artifacts: Iterable[tuple[str, str, str]],
    *,
    created_by: str,
) -> None:
    """Write wheel identities as pylock 1.0 archive records in input order."""

    from pip._vendor.packaging.utils import parse_wheel_filename

    print('lock-version = "1.0"', file=stream)
    print(
        f"created-by = {json.dumps(created_by)}",  # machine-string: pylock values cannot contain debug expression names
        file=stream,
    )

    for (wheel_name, checksum, url) in artifacts:
        (distribution, version, _, _) = parse_wheel_filename(wheel_name)

        print("[[packages]]", file=stream)
        print(
            f"name = {json.dumps(str(distribution))}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )
        print(
            f"version = {json.dumps(str(version))}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )

        print("[packages.archive]", file=stream)
        print(
            f"url = {json.dumps(url)}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )

        print("[packages.archive.hashes]", file=stream)
        print(
            f"sha256 = {json.dumps(checksum)}",  # machine-string: pylock values cannot contain debug expression names
            file=stream,
        )
