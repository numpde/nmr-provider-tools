"""Admit inert Upload observations from the pinned provider API wire contract.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/schemas/job_upload_set_read_response.v1.schema.json,
wire/upload_read_capability_response.py, wire/upload_transfer_schemas.py and
json_datetime.py. A grant is not proof of membership freshness or downloaded
bytes. The acquisition owner must check configured store origin before use.
"""

from provider_core.container_guard import require_container

require_container()

import re
from dataclasses import dataclass, field
from datetime import datetime

from provider_core.response_json import ResponseRejected, read_document

# The pinned upload membership response has this byte ceiling.
# repo-check: allow-module-constant
MEMBERSHIP_RESPONSE_BYTES = 2097152
# The pinned upload grant response has this byte ceiling.
# repo-check: allow-module-constant
GRANT_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class UploadMember:
    upload_ref: str
    description: str = field(repr=False)
    byte_length: int
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class UploadSet:
    """One observed Job membership, not an atomic freeze of later membership."""

    job_ref: str
    members: tuple[UploadMember, ...]


@dataclass(frozen=True, slots=True)
class ReadGrant:
    """Inert access data; job_ref records request context, not an echoed fact."""

    job_ref: str
    upload_ref: str
    byte_length: int
    content_hash: str
    download_url: str = field(repr=False)
    capability: str = field(repr=False)
    expires_at: datetime


def require_job_ref(value):
    if type(value) is not str or re.fullmatch(r"job:[A-Za-z0-9_.-]{1,124}", value) is None:
        raise ValueError("Job reference must be job: followed by 1 through 124 letters, digits, underscores, dots or hyphens")


def require_upload_ref(value):
    if type(value) is not str or re.fullmatch(r"upload:sha256:[0-9a-f]{64}", value) is None:
        raise ResponseRejected("Upload reference must be upload:sha256: followed by 64 lowercase hexadecimal digits")


def _fields(document, names):
    if type(document) is not dict or set(document) != names:
        raise ResponseRejected("Response fields do not match the supported Upload wire schema")


def _byte_facts(document, *, pending_allowed):
    require_upload_ref(document["upload_ref"])
    length = document["byte_length"]
    if type(length) is not int or not 1 <= length <= 9223372036854775807:
        raise ResponseRejected("Upload byte_length must be a positive signed 64-bit integer")
    fingerprint = document["content_hash"]
    if pending_allowed and fingerprint is None:
        return
    if type(fingerprint) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
        raise ResponseRejected("Upload content_hash must be a lowercase SHA-256 fingerprint")


def read_membership(raw, *, job_ref):
    """Preserve ordered descriptions and pending hashes after exact Job binding."""
    document = read_document(raw, MEMBERSHIP_RESPONSE_BYTES)
    _fields(document, {"schema_id", "job_ref", "uploads"})
    if document["schema_id"] != "nmr.provider.job_upload_set.read.response.v1" or document["job_ref"] != job_ref:
        raise ResponseRejected("Membership response schema or Job identity differs from the request")
    members = document["uploads"]
    if type(members) is not list or len(members) > 64:
        raise ResponseRejected("Membership must contain an array of at most 64 Uploads")
    admitted = []
    for member in members:
        _fields(member, {"upload_ref", "description", "byte_length", "content_hash"})
        _byte_facts(member, pending_allowed=True)
        description = member["description"]
        if (
            type(description) is not str
            or len(description) > 4096
            or any(c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in description)
        ):
            raise ResponseRejected("Upload description must contain at most 4096 Unicode scalar values without NUL")
        if admitted and member["upload_ref"] <= admitted[-1].upload_ref:
            raise ResponseRejected("Membership must contain unique Upload references in ascending order")
        admitted.append(UploadMember(member["upload_ref"], description, member["byte_length"], member["content_hash"]))
    return UploadSet(job_ref, tuple(admitted))


def _expiry(value):
    # Match the API renderer: UTC seconds, or exactly six nonzero fraction digits.
    if (
        type(value) is not str
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?!000000)[0-9]{6})?Z", value) is None
    ):
        raise ResponseRejected("Grant expiry must be canonical UTC seconds or six nonzero fractional digits")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ResponseRejected("Grant expiry is not a valid calendar date and time") from None


def read_grant(raw, *, job_ref, upload_ref):
    """Bind the returned resource; access scope and byte verification follow later."""
    document = read_document(raw, GRANT_RESPONSE_BYTES)
    _fields(
        document, {"schema_id", "upload_ref", "method", "download_url", "byte_length", "content_hash", "capability", "expires_at"}
    )
    if document["schema_id"] != "nmr.upload.read_capability.response.v1" or document["upload_ref"] != upload_ref:
        raise ResponseRejected("Grant response schema or Upload identity differs from the request")
    if document["method"] != "GET":
        raise ResponseRejected("Download grant must authorize GET")
    _byte_facts(document, pending_allowed=False)
    url = document["download_url"]
    if type(url) is not str or not url.startswith("https://") or len(url) > 359:
        raise ResponseRejected("Grant download_url must be an HTTPS URL of at most 359 characters")
    token = document["capability"]
    if type(token) is not str or not 1 <= len(token) <= 2048:
        raise ResponseRejected("Grant capability must contain 1 through 2048 characters")
    return ReadGrant(
        job_ref, upload_ref, document["byte_length"], document["content_hash"], url, token, _expiry(document["expires_at"])
    )
