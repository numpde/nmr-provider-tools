"""Synthetic Upload wire facts from the pinned API schemas in note 017."""

import json

UPLOAD_REF = "upload:sha256:" + "a" * 64
CONTENT_HASH = "sha256:" + "b" * 64


def member(**changes):
    return {
        "upload_ref": UPLOAD_REF,
        "description": "Two conformers\n雪",
        "byte_length": 7,
        "content_hash": CONTENT_HASH,
    } | changes


def membership(**changes):
    return {
        "schema_id": "nmr.provider.job_upload_set.read.response.v1",
        "job_ref": "job:example",
        "uploads": [member()],
    } | changes


def grant(**changes):
    return {
        "schema_id": "nmr.upload.read_capability.response.v1",
        "upload_ref": UPLOAD_REF,
        "method": "GET",
        "byte_length": 7,
        "content_hash": CONTENT_HASH,
        "download_url": "https://store.example/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
        "capability": "private-bearer-canary",
        "expires_at": "2026-10-19T00:00:00Z",
    } | changes


def encoded(document):
    return json.dumps(document, ensure_ascii=True).encode()
