"""Construct Simple JSON v1 requests and resolve their artifact links.

https://packaging.python.org/en/latest/specifications/simple-repository-api/
"""

import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any


def project_request(index: str, project_name: str) -> urllib.request.Request:
    """Request the caller-selected normalized project using the existing media type."""

    return urllib.request.Request(
        f"{index}/{project_name}/",  # machine-string: Simple API projects use normalized-name path segments
        headers={"Accept": "application/vnd.pypi.simple.v1+json"},
    )


def file_url(project_url: str, artifact: Mapping[str, Any]) -> str:
    """Resolve the selected file's link without admitting its download location."""

    return urllib.parse.urljoin(project_url, artifact["url"])
