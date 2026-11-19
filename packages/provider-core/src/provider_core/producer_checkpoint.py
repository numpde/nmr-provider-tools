"""Bound live control projections without retaining scientific payloads.

Encoding measures the complete control allowance; it does not create durable
recovery state. Providers retain the actual controllers only for their lifetime.
"""

from provider_core.container_guard import require_container

require_container()

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from provider_core.adapters.nmr.v1.attempt_response import start_admission_body
from provider_core.attempt_record import (
    MAX_PENDING_RECORD_BYTES,
    ProtectedEvidence,
    RecoveryScope,
    StartPending,
    SuppressionReason,
    TerminalOperation,
    _encode_pending,
)

from provider_core.validation.evidence import require_evidence_schema_id
from provider_core.validation.upstream.nmr.v1.attempt_response import ATTEMPT_RESPONSE_BYTES, AttemptAdmission, attempt_is_in_progress, read_start
from provider_core.type_requirements import require_type

# The existing START/admission bounds plus a 1024-scalar schema identifier,
# digest/size and fixed state labels; there is no future result-body reservation.
# Bound the control projection independently of scientific payload size.
# repo-check: allow-module-constant
MAX_CHECKPOINT_BYTES = MAX_PENDING_RECORD_BYTES + ATTEMPT_RESPONSE_BYTES + 16384

# Identify the bounded control projection used to qualify live metadata size.
# repo-check: allow-module-constant
SCHEMA = "provider_core.producer_checkpoint.v1"


# silent-defaults: allow-contract -- Production retains its existing shared volume names; a contained recipe may supply its admitted private project.
def checkpoint_volume_name(scope, *, namespace="nmr-api-delivery"):
    """Keep producer control private to its original API/provider/lane/generation."""

    require_type(scope, RecoveryScope)

    raw = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":")).encode()

    return namespace + "-control-" + hashlib.sha256(raw).hexdigest()


def _require_digest(value):
    require_type(value, str)

    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("A control checkpoint digest must be a canonical SHA-256 identity.")


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    schema_id: str
    sha256: str
    byte_length: int

    def __post_init__(self):
        require_evidence_schema_id(self.schema_id)
        _require_digest(self.sha256)

        require_type(self.byte_length, int)

        if not (1 <= self.byte_length):
            raise ValueError("A prepared identity digest requires its positive byte length.")

    @classmethod
    def from_evidence(cls, evidence):
        require_type(evidence, ProtectedEvidence)

        return cls(evidence.schema_id, "sha256:" + hashlib.sha256(evidence.content).hexdigest(), len(evidence.content))


@dataclass(frozen=True, slots=True)
class AcknowledgedCheckpoint:
    """Scoped START admission without a scientific execution handle."""

    start: StartPending
    admission: AttemptAdmission

    def __post_init__(self):
        require_type(self.start, StartPending)

        read_start(
            start_admission_body(self.admission),
            job_ref=self.start.input.job_ref,
            analysis_kind_ref=self.start.analysis_kind_ref,
            provider_ref=self.start.scope.provider_ref
        )


@dataclass(frozen=True, slots=True)
class EnteredCheckpoint:
    acknowledgement: AcknowledgedCheckpoint
    prepared: EvidenceDigest

    def __post_init__(self):
        require_type(self.acknowledgement, AcknowledgedCheckpoint)

        if not attempt_is_in_progress(self.acknowledgement.admission):
            raise ValueError("Execution entry requires an active acknowledgement and only its prepared identity digest.")

        require_type(self.prepared, EvidenceDigest)


def checkpoint_acknowledgement(checkpoint):
    if type(checkpoint) is AcknowledgedCheckpoint:
        return checkpoint

    if type(checkpoint) is EnteredCheckpoint:
        return checkpoint.acknowledgement

    if type(checkpoint) is TerminalSelectedCheckpoint:
        return checkpoint_acknowledgement(checkpoint.attempt)

    if type(checkpoint) is DeliveryAcceptedCheckpoint:
        return checkpoint_acknowledgement(checkpoint.selection)

    if type(checkpoint) is SuppressedCheckpoint:
        return checkpoint_acknowledgement(checkpoint.attempt)

    raise ValueError("This control checkpoint has no acknowledged Attempt.")


def _require_active(checkpoint):
    if not (type(checkpoint) in (expected_type := (AcknowledgedCheckpoint, EnteredCheckpoint))):
        raise TypeError(f"{type(checkpoint)=} ≠ {expected_type=}")

    if not attempt_is_in_progress(checkpoint_acknowledgement(checkpoint).admission):
        raise ValueError("A terminal START replay grants no execution or fresh terminal selection.")


@dataclass(frozen=True, slots=True)
class TerminalSelectedCheckpoint:
    """The approved immutable command identity before a delivery handoff."""

    attempt: AcknowledgedCheckpoint | EnteredCheckpoint
    operation: TerminalOperation
    body_sha256: str
    body_bytes: int

    def __post_init__(self):
        _require_active(self.attempt)

        require_type(self.operation, TerminalOperation)

        _require_digest(self.body_sha256)

        require_type(self.body_bytes, int)

        if not (1 <= self.body_bytes):
            raise ValueError("Terminal selection requires the chosen command's positive byte length.")


@dataclass(frozen=True, slots=True)
class DeliveryAcceptedCheckpoint:
    """Historical durable relay handoff; not an API receipt or cleanup proof."""

    selection: TerminalSelectedCheckpoint

    def __post_init__(self):
        require_type(self.selection, TerminalSelectedCheckpoint)


@dataclass(frozen=True, slots=True)
class SuppressedCheckpoint:
    """Retain why a candidate was not selected, without persisting that candidate."""

    attempt: AcknowledgedCheckpoint | EnteredCheckpoint
    reason: SuppressionReason

    def __post_init__(self):
        _require_active(self.attempt)

        require_type(self.reason, SuppressionReason)




def _document(checkpoint):
    if type(checkpoint) is StartPending:
        return {"kind": "start_pending", "start": json.loads(_encode_pending(checkpoint))}

    if type(checkpoint) is AcknowledgedCheckpoint:
        return {
            "kind": "acknowledged",
            "start": json.loads(_encode_pending(checkpoint.start)),
            "admission": json.loads(start_admission_body(checkpoint.admission))
        }

    if type(checkpoint) is EnteredCheckpoint:
        return {"kind": "entered", "acknowledgement": _document(checkpoint.acknowledgement), "prepared": asdict(checkpoint.prepared)}

    if type(checkpoint) is TerminalSelectedCheckpoint:
        return {
            "kind": "terminal_selected",
            "attempt": _document(checkpoint.attempt),
            "operation": checkpoint.operation.value,
            "body_sha256": checkpoint.body_sha256,
            "body_bytes": checkpoint.body_bytes
        }

    if type(checkpoint) is DeliveryAcceptedCheckpoint:
        return {"kind": "delivery_accepted", "selection": _document(checkpoint.selection)}

    if type(checkpoint) is SuppressedCheckpoint:
        return {"kind": "suppressed", "attempt": _document(checkpoint.attempt), "reason": checkpoint.reason.value}

    raise ValueError("Cannot encode this value as a producer control checkpoint; scientific records are not accepted.")


def encode_checkpoint(checkpoint):
    raw = json.dumps({"schema_id": SCHEMA, **_document(checkpoint)}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    if not (len(raw) <= MAX_CHECKPOINT_BYTES):
        raise ValueError("Producer control checkpoint exceeds its metadata byte limit.")

    return raw
