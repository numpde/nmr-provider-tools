"""Admit inert Upload observations from the selected Provider API wire contract.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/schemas/job_upload_set_read_response.v1.schema.json,
wire/upload_read_capability_response.py, wire/upload_transfer_schemas.py and
json_datetime.py. A grant is not proof of membership freshness or downloaded
bytes. The acquisition owner must check configured store origin before use.
"""

from provider_core.container_guard import require_container

require_container()

import re
import reprlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from provider_core.response_json import ResponseRejected, read_document
from provider_core.type_requirements import require_type
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.validation.upstream.nmr.v1.timestamps import read_timestamp

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


def require_upload_ref(value):
    require_type(value, str, error_factory=ResponseRejected)

    if re.fullmatch(r"upload:sha256:[0-9a-f]{64}", value) is None:
        raise ResponseRejected("Upload reference must be upload:sha256: followed by 64 lowercase hexadecimal digits")


def _fields(upload_fields, expected_fields):
    require_type(upload_fields, dict, error_factory=ResponseRejected)

    if (received_fields := set(upload_fields)) != expected_fields:
        raise ResponseRejected(f"{received_fields=} should match {expected_fields=}")


def _byte_facts(upload_fields, *, pending_allowed):
    require_upload_ref(upload_fields["upload_ref"])
    upload_byte_length = upload_fields["byte_length"]

    require_type(upload_byte_length, int, error_factory=ResponseRejected)

    (min_upload_bytes, max_upload_bytes) = (1, 9223372036854775807)

    if not (min_upload_bytes <= upload_byte_length <= max_upload_bytes):
        raise ResponseRejected(f"upload_byte_length should be between {min_upload_bytes=} and {max_upload_bytes=}")

    content_hash = upload_fields["content_hash"]

    if pending_allowed and (content_hash is None):
        return

    require_type(content_hash, str, error_factory=ResponseRejected)

    if re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash) is None:
        raise ResponseRejected(f"{reprlib.repr(content_hash)=} should be 'sha256:' followed by 64 lowercase hexadecimal digits")


def _grant_target_and_token(download_url, token):
    require_type(download_url, str, error_factory=ResponseRejected)

    if not download_url.startswith(expected_prefix := "https://"):
        raise ResponseRejected(f"{download_url=} should start with {expected_prefix=}")

    if not (len(download_url) <= (maximum_url_characters := 359)):
        raise ResponseRejected(f"{len(download_url)=} exceeds {maximum_url_characters=}")

    require_type(token, str, error_factory=ResponseRejected)

    if not (1 <= len(token) <= 2048):
        raise ResponseRejected("Grant capability must contain 1 through 2048 characters")


def require_read_grant(grant, *, job_ref):
    """Admit a caller-supplied grant's facts without claiming API issuance."""
    require_job_ref(job_ref)

    require_type(grant, ReadGrant, error_factory=ResponseRejected)

    if grant.job_ref != job_ref:
        raise ResponseRejected("Read grant must belong to the selected Job")

    _byte_facts(
        {
            "upload_ref": grant.upload_ref,
            "byte_length": grant.byte_length,
            "content_hash": grant.content_hash
        },
        pending_allowed=False
    )

    _grant_target_and_token(grant.download_url, grant.capability)

    require_type((actual_expires_at := grant.expires_at), datetime, error_factory=ResponseRejected)

    if grant.expires_at.tzinfo is None:
        raise ResponseRejected("Read grant expiry must be a UTC datetime")

    if grant.expires_at.utcoffset() != timedelta(0):
        raise ResponseRejected("Read grant expiry must be a UTC datetime")


def read_membership(raw, *, job_ref):
    """Preserve ordered descriptions and pending hashes after exact Job binding."""
    membership_response = read_document(raw, MEMBERSHIP_RESPONSE_BYTES)

    _fields(membership_response, {"schema_id", "job_ref", "uploads"})

    if (membership_response["schema_id"] != "nmr.provider.job_upload_set.read.response.v1") or (membership_response["job_ref"] != job_ref):
        raise ResponseRejected("Membership response schema or Job identity differs from the request")

    members = membership_response["uploads"]

    require_type(members, list, error_factory=ResponseRejected)

    if len(members) > 64:
        raise ResponseRejected("Membership must contain an array of at most 64 Uploads")

    admitted = []

    for member in members:
        _fields(member, {"upload_ref", "description", "byte_length", "content_hash"})
        _byte_facts(member, pending_allowed=True)
        description = member["description"]

        require_type(description, str, error_factory=ResponseRejected)

        if len(description) > 4096:
            raise ResponseRejected("Upload description must contain at most 4096 Unicode scalar values without NUL")

        # API descriptions forbid NUL and surrogate code points (U+D800–U+DFFF)
        if any(((c == "\x00") or (0xD800 <= ord(c) <= 0xDFFF)) for c in description):
            raise ResponseRejected("Upload description must contain at most 4096 Unicode scalar values without NUL")

        if admitted and (member["upload_ref"] <= admitted[-1].upload_ref):
            raise ResponseRejected("Membership must contain unique Upload references in ascending order")

        admitted.append(UploadMember(member["upload_ref"], description, member["byte_length"], member["content_hash"]))

    return UploadSet(job_ref, tuple(admitted))


def read_grant(raw, *, job_ref, upload_ref):
    """Bind the returned resource; access scope and byte verification follow later."""
    grant_response = read_document(raw, GRANT_RESPONSE_BYTES)

    _fields(
        grant_response,
        {"schema_id", "upload_ref", "method", "download_url", "byte_length", "content_hash", "capability", "expires_at"}
    )

    if (grant_response["schema_id"] != "nmr.upload.read_capability.response.v1") or (grant_response["upload_ref"] != upload_ref):
        raise ResponseRejected("Grant response schema or Upload identity differs from the request")

    if grant_response["method"] != "GET":
        raise ResponseRejected("Download grant must authorize GET")

    _byte_facts(grant_response, pending_allowed=False)
    download_url = grant_response["download_url"]
    capability = grant_response["capability"]

    _grant_target_and_token(download_url, capability)

    return ReadGrant(
        job_ref,
        upload_ref,
        grant_response["byte_length"],
        grant_response["content_hash"],
        download_url,
        capability,
        read_timestamp(grant_response["expires_at"])
    )
