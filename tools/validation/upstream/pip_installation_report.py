"""Read the pip installation report v1 profile used by offline build workers."""

from tools.container_guard import require_container

require_container()

import reprlib
from collections.abc import Mapping
from typing import Any


def install_records(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the selected version's install records without opening artifacts."""

    report_version = report["version"]
    expected_version = "1"

    if not (report_version == expected_version):
        raise ValueError(f"{reprlib.repr(report_version)=} should match {expected_version=}")

    return report["install"]


def artifact_url(record: Mapping[str, Any]) -> str:
    """Read the reported location before caller-owned source admission."""

    return record["download_info"]["url"]


def archive_sha256(record: Mapping[str, Any]) -> str:
    """Read hash evidence when the caller has admitted the selected artifact."""

    return record["download_info"]["archive_info"]["hashes"]["sha256"]
