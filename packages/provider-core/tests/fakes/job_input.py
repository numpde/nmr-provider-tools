"""Synthetic response shaped from the pinned API schema in the package README."""

import base64
import hashlib
import json

from provider_core.job_input import FeedInputFacts


# silent-defaults: allow-contract -- The synthetic input body and its fingerprint describe the same fixture bytes.
def response(payload=b"private instructions", **changes):
    document = {
        "schema_id": "nmr.provider.job_input.read.response.v1",
        "job_ref": "job:example",
        "input_schema_id": "nmr.job.specification.text.v1",
        "input_fingerprint": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "input_byte_length": len(payload),
        "canonical_input_base64": base64.b64encode(payload).decode("ascii"),
    }
    document.update(changes)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


# silent-defaults: allow-contract -- The synthetic input response describes its selected fixture bytes and limits.
def facts(payload=b"private instructions"):
    return FeedInputFacts(
        job_ref="job:example",
        input_schema_id="nmr.job.specification.text.v1",
        input_fingerprint="sha256:" + hashlib.sha256(payload).hexdigest(),
        input_byte_length=len(payload),
    )
