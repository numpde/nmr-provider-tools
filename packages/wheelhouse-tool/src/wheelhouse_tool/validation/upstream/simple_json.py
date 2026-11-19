"""Read the existing Simple JSON v1 subset without choosing approved artifacts.

https://packaging.python.org/en/latest/specifications/simple-repository-api/
This retains the tool's consumed profile, including incomplete-file skips. It
does not add full-schema validation or change the selected API version.
"""

import json
import reprlib
from collections.abc import Mapping
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version


def project_files(raw: bytes, *, expected_project_name: str) -> list[Mapping[str, Any]]:
    """Decode the matching project and return its files in received order."""

    try:
        project = json.loads(raw)
    except ValueError as cause:
        raise ValueError(f"Cannot decode metadata for {reprlib.repr(expected_project_name)=}: {cause}") from cause

    project_name = canonicalize_name(project["name"])

    if not (project_name == expected_project_name):
        raise ValueError(f"{reprlib.repr(project_name)=} should match {reprlib.repr(expected_project_name)=}")

    # silent-defaults: allow-contract -- Preserve the existing consumed profile's empty result when files is absent
    return project.get("files", [])


def file_identity(artifact: Mapping[str, Any]) -> tuple[str, str | None] | None:
    """Read filename and SHA-256, preserving existing incomplete-identity skips."""

    if "filename" not in artifact:
        return None

    filename = artifact["filename"]
    artifact_hashes = artifact.get("hashes")

    if artifact_hashes is None:
        return None

    return (filename, artifact_hashes.get("sha256"))


def supports_python(artifact: Mapping[str, Any], python_version: Version) -> bool:
    """Interpret Requires-Python only after the caller admits the file's hash."""

    requires = artifact.get("requires-python")

    return not requires or SpecifierSet(requires).contains(python_version, prereleases=True)


def file_yanked(artifact: Mapping[str, Any]) -> bool:
    """Interpret the existing boolean-or-reason marker without choosing a file."""

    return bool(artifact.get("yanked"))
