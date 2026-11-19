"""Request the existing PyPI project/version JSON profile."""

from tools.container_guard import require_container

require_container()

import json
import urllib.request
from typing import Any


def release_metadata(distribution: str, version: str, *, timeout_seconds: float) -> dict[str, Any]:
    """Read the selected release using the caller's network timeout and proxy."""

    # https://docs.pypi.org/api/json/#get-release
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{distribution}/{version}/json",  # machine-string: PyPI routes by distribution and version path segments
        timeout=timeout_seconds,
    ) as response:
        return json.load(response)
