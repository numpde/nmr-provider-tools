"""Encode retained START admission for validation against its original binding.

The record owner retains the acknowledgement and local execution phase. This
codec supplies the API representation; it neither persists nor authorizes work.
"""

from provider_core.container_guard import require_container

require_container()

import json
from dataclasses import asdict

from provider_core.type_requirements import require_type
from provider_core.validation.upstream.nmr.v1.attempt_response import AttemptAdmission, require_start_timestamp


def start_admission_body(admission: AttemptAdmission) -> bytes:
    """Render the retained acknowledgement without creating another admission."""

    require_type(admission, AttemptAdmission)

    started_at = admission.started_at

    require_start_timestamp(started_at)

    start_response = asdict(admission)
    start_response["started_at"] = started_at.isoformat().replace("+00:00", "Z")
    start_response["schema_id"] = "nmr.provider.execution_attempt_start_response.v1"

    return json.dumps(start_response, sort_keys=True, separators=(",", ":")).encode()
