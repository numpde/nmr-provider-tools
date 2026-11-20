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

    if not re.fullmatch(r"upload:sha256:[0-9a-f]{64}", value):
        raise ResponseRejected("Upload reference must be upload:sha256: followed by 64 lowercase hexadecimal digits")


def _fields(upload_fields, expected_fields):
    require_type(upload_fields, dict, error_factory=ResponseRejected)

    if not ((received_fields := set(upload_fields)) == expected_fields):
        raise ResponseRejected(f"{received_fields=} should match {expected_fields=}")


def _byte_facts(upload_fields, *, pending_allowed):
    require_upload_ref(upload_fields["upload_ref"])
    upload_byte_length = upload_fields["byte_length"]

    require_type(upload_byte_length, int, error_factory=ResponseRejected)

    (min_upload_bytes, max_upload_bytes) = (1, 9223372036854775807)

    if not (min_upload_bytes <= upload_byte_length <= max_upload_bytes):
        raise ResponseRejected(f"Expected {min_upload_bytes=} ≤ {upload_byte_length=} ≤ {max_upload_bytes=}")

    content_hash = upload_fields["content_hash"]

    if pending_allowed and (content_hash is None):
        return

    require_type(content_hash, str, error_factory=ResponseRejected)

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
        raise ResponseRejected(f"{reprlib.repr(content_hash)=} should be 'sha256:' followed by 64 lowercase hexadecimal digits")


def _require_download_access(download_url, capability):
    require_type(download_url, str, error_factory=ResponseRejected)

    if not download_url.startswith(expected_prefix := "https://"):
        raise ResponseRejected(f"{reprlib.repr(download_url)=} should start with {expected_prefix=}")

    if not (len(download_url) <= (maximum_url_characters := 359)):
        raise ResponseRejected(f"{len(download_url)=} exceeds {maximum_url_characters=}")

    require_type(capability, str, error_factory=ResponseRejected)

    min_capability_characters = 1
    max_capability_characters = 2048

    if not (min_capability_characters <= len(capability) <= max_capability_characters):
        raise ResponseRejected(f"Expected {min_capability_characters=} ≤ {len(capability)=} ≤ {max_capability_characters=}")


def require_read_grant(grant, *, job_ref):
    """Admit a caller-supplied grant's facts without claiming API issuance."""
    require_job_ref(job_ref)

    require_type(grant, ReadGrant, error_factory=ResponseRejected)

    if not (grant.job_ref == job_ref):
        raise ResponseRejected(f"{reprlib.repr(grant.job_ref)=} should match {job_ref=}")

    _byte_facts(
        {
            "upload_ref": grant.upload_ref,
            "byte_length": grant.byte_length,
            "content_hash": grant.content_hash
        },
        pending_allowed=False
    )

    _require_download_access(grant.download_url, grant.capability)

    require_type((actual_expires_at := grant.expires_at), datetime, error_factory=ResponseRejected)

    if not (grant.expires_at.tzinfo is not None):
        raise ResponseRejected(f"{grant.expires_at=} should include a timezone")

    utc_offset = grant.expires_at.utcoffset()
    expected_utc_offset = timedelta(0)

    if not (utc_offset == expected_utc_offset):
        raise ResponseRejected(f"{utc_offset=} should match {expected_utc_offset=}")


def read_membership(raw, *, job_ref):
    """Preserve ordered descriptions and pending hashes after exact Job binding."""
    membership_response = read_document(raw, MEMBERSHIP_RESPONSE_BYTES)

    _fields(membership_response, {"schema_id", "job_ref", "uploads"})

    expected_schema_id = "nmr.provider.job_upload_set.read.response.v1"

    if not ((membership_response["schema_id"] == expected_schema_id) and (membership_response["job_ref"] == job_ref)):
        raise ResponseRejected(
            f"{reprlib.repr(membership_response['schema_id'])=} should match {expected_schema_id=}; "
            f"{reprlib.repr(membership_response['job_ref'])=} should match {job_ref=}"
        )

    members = membership_response["uploads"]

    require_type(members, list, error_factory=ResponseRejected)

    max_upload_member_count = 64

    if not (len(members) <= max_upload_member_count):
        raise ResponseRejected(f"{len(members)=} exceeds {max_upload_member_count=}")

    upload_members = []
    max_description_characters = 4096

    for member in members:
        _fields(member, {"upload_ref", "description", "byte_length", "content_hash"})
        _byte_facts(member, pending_allowed=True)
        description = member["description"]

        require_type(description, str, error_factory=ResponseRejected)

        if not (len(description) <= max_description_characters):
            raise ResponseRejected(f"{len(description)=} exceeds {max_description_characters=}")

        # API descriptions forbid NUL and surrogate code points (U+D800–U+DFFF)
        for description_character in description:
            if not ((description_character != "\x00") and (not (0xD800 <= ord(description_character) <= 0xDFFF))):
                raise ResponseRejected(f"{description_character=} should be a non-NUL Unicode scalar")

        if not ((not (upload_members)) or (member["upload_ref"] > upload_members[-1].upload_ref)):
            raise ResponseRejected(f"{member['upload_ref']=} should be greater than {upload_members[-1].upload_ref=}")

        upload_members.append(UploadMember(member["upload_ref"], description, member["byte_length"], member["content_hash"]))

    return UploadSet(job_ref, tuple(upload_members))


def read_grant(raw, *, job_ref, upload_ref):
    """Bind the returned resource; access scope and byte verification follow later."""
    grant_response = read_document(raw, GRANT_RESPONSE_BYTES)

    _fields(
        grant_response,
        {"schema_id", "upload_ref", "method", "download_url", "byte_length", "content_hash", "capability", "expires_at"}
    )

    expected_schema_id = "nmr.upload.read_capability.response.v1"

    if not ((grant_response["schema_id"] == expected_schema_id) and (grant_response["upload_ref"] == upload_ref)):
        raise ResponseRejected(
            f"{reprlib.repr(grant_response['schema_id'])=} should match {expected_schema_id=}; "
            f"{reprlib.repr(grant_response['upload_ref'])=} should match {upload_ref=}"
        )

    expected_method = "GET"

    if not (grant_response["method"] == expected_method):
        raise ResponseRejected(f"{reprlib.repr(grant_response['method'])=} should match {expected_method=}")

    _byte_facts(grant_response, pending_allowed=False)
    download_url = grant_response["download_url"]
    capability = grant_response["capability"]

    _require_download_access(download_url, capability)

    return ReadGrant(
        job_ref,
        upload_ref,
        grant_response["byte_length"],
        grant_response["content_hash"],
        download_url,
        capability,
        read_timestamp(grant_response["expires_at"])
    )
