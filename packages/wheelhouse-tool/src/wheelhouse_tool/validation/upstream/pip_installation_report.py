"""Read the pip installation report v1 fields used by the standalone tool."""

import reprlib
from collections.abc import Mapping
from typing import Any


def install_records(report: object) -> list[Any]:
    """Read the versioned install list without inspecting individual records."""

    if not isinstance(report, (expected_report_type := dict)):
        raise ValueError(f"{type(report)=} ≠ {expected_report_type=}")

    report_version = report.get("version")
    expected_version = "1"

    if report_version != expected_version:
        raise ValueError(f"{reprlib.repr(report_version)=} ≠ {expected_version=}")

    install_records = report.get("install")

    if not isinstance(install_records, (expected_records_type := list)):
        raise ValueError(f"{type(install_records)=} ≠ {expected_records_type=}")

    return install_records


def artifact_url(record: Mapping[str, Any]) -> str:
    """Read the reported location before caller-owned source admission."""

    return record["download_info"]["url"]


def archive_sha256(record: Mapping[str, Any]) -> str:
    """Read hash evidence when the caller has admitted the selected artifact."""

    return record["download_info"]["archive_info"]["hashes"]["sha256"]
