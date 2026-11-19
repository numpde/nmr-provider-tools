"""Admit the Debian archive filename used for an ordinary binary package."""

from tools.container_guard import require_container

require_container()

import reprlib


def require_binary_package_filename(
    filename: str,
    *,
    package_name: str,
    package_version: str,
    architecture: str,
) -> None:
    """Require the archive spelling, which omits the version epoch."""

    # https://manpages.debian.org/bookworm/dpkg-dev/dpkg-name.1.en.html
    archive_version = package_version.split(":", 1)[-1]
    expected_filename = f"{package_name}_{archive_version}_{architecture}.deb"  # machine-string: Debian binary archive filename

    if not (filename == expected_filename):
        raise ValueError(f"{reprlib.repr(filename)=} should match {reprlib.repr(expected_filename)=}")
