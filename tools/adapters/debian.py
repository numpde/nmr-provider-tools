"""Invoke the selected dpkg install and version query commands."""

from tools.container_guard import require_container

require_container()

import pathlib
import subprocess


def install_binary_packages(paths: list[pathlib.Path]) -> None:
    """Install the caller's verified package paths in one command."""

    subprocess.run(
        [
            "dpkg",
            "--install",  # Unpack and configure the supplied archives
            *[str(path) for path in paths],
        ],
        check=True,
    )


def installed_package_version(package_name: str) -> str:
    """Return the exact Version field rendered by dpkg-query."""

    # https://manpages.debian.org/bookworm/dpkg/dpkg-query.1.en.html
    return subprocess.run(
        [
            "dpkg-query",
            "--show",
            "--showformat=${Version}",  # Emit the version field alone, without a newline
            package_name,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
