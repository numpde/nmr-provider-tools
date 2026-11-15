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

from provider_core.attempt_record import (
    MAX_PENDING_RECORD_BYTES,
    ProtectedEvidence,
    RecoveryScope,
    StartPending,
    SuppressionReason,
    TerminalOperation,
    _admission_bytes,
    _encode_pending,
)

from provider_core.attempt_request import _scalar_text
from provider_core.attempt_response import ATTEMPT_RESPONSE_BYTES, AttemptAdmission, read_start

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

    if not (type(scope) is (expected_type := RecoveryScope)):
        raise TypeError(
            f"{type(scope)=} ≠ {expected_type=}"
        )

    raw = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":")).encode()

    return namespace + "-control-" + hashlib.sha256(raw).hexdigest()


def _require_digest(value):
    if not (type(value) is (expected_type := str)):
        raise TypeError(
            f"{type(value)=} ≠ {expected_type=}"
        )

    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("A control checkpoint digest must be a canonical SHA-256 identity.")


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    schema_id: str
    sha256: str
    byte_length: int

    def __post_init__(self):
        _scalar_text(self.schema_id, "prepared identity schema")
        _require_digest(self.sha256)

        if not (type((actual_byte_length := self.byte_length)) is (expected_type := int)):
            raise TypeError(
                f"{type(actual_byte_length)=} ≠ {expected_type=}"
            )

        if not (1 <= self.byte_length):
            raise ValueError("A prepared identity digest requires its positive byte length.")

    @classmethod
    def from_evidence(cls, evidence):
        if not (type(evidence) is (expected_type := ProtectedEvidence)):
            raise TypeError(
                f"{type(evidence)=} ≠ {expected_type=}"
            )

        return cls(evidence.schema_id, "sha256:" + hashlib.sha256(evidence.content).hexdigest(), len(evidence.content))


@dataclass(frozen=True, slots=True)
class AcknowledgedCheckpoint:
    """Scoped START admission without a scientific execution handle."""

    start: StartPending
    admission: AttemptAdmission

    def __post_init__(self):
        if not (type((actual_start := self.start)) is (expected_type := StartPending)):
            raise TypeError(
                f"{type(actual_start)=} ≠ {expected_type=}"
            )

        read_start(_admission_bytes(self.admission),
                   job_ref=self.start.input.job_ref,
                   analysis_kind_ref=self.start.analysis_kind_ref,
                   provider_ref=self.start.scope.provider_ref)


@dataclass(frozen=True, slots=True)
class EnteredCheckpoint:
    acknowledgement: AcknowledgedCheckpoint
    prepared: EvidenceDigest

    def __post_init__(self):
        if not (type((actual_acknowledgement := self.acknowledgement)) is (expected_type := AcknowledgedCheckpoint)):
            raise TypeError(
                f"{type(actual_acknowledgement)=} ≠ {expected_type=}"
            )

        if self.acknowledgement.admission.state != "in_progress":
            raise ValueError("Execution entry requires an active acknowledgement and only its prepared identity digest.")

        if not (type((actual_prepared := self.prepared)) is (expected_type := EvidenceDigest)):
            raise TypeError(
                f"{type(actual_prepared)=} ≠ {expected_type=}"
            )


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

    if checkpoint_acknowledgement(checkpoint).admission.state != "in_progress":
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

        if not (type((actual_operation := self.operation)) is (expected_type := TerminalOperation)):
            raise TypeError(
                f"{type(actual_operation)=} ≠ {expected_type=}"
            )

        _require_digest(self.body_sha256)

        if not (type((actual_body_bytes := self.body_bytes)) is (expected_type := int)):
            raise TypeError(
                f"{type(actual_body_bytes)=} ≠ {expected_type=}"
            )

        if not (1 <= self.body_bytes):
            raise ValueError("Terminal selection requires the chosen command's positive byte length.")


@dataclass(frozen=True, slots=True)
class DeliveryAcceptedCheckpoint:
    """Historical durable relay handoff; not an API receipt or cleanup proof."""

    selection: TerminalSelectedCheckpoint

    def __post_init__(self):
        if not (type((actual_selection := self.selection)) is (expected_type := TerminalSelectedCheckpoint)):
            raise TypeError(
                f"{type(actual_selection)=} ≠ {expected_type=}"
            )


@dataclass(frozen=True, slots=True)
class SuppressedCheckpoint:
    """Retain why a candidate was not selected, without persisting that candidate."""

    attempt: AcknowledgedCheckpoint | EnteredCheckpoint
    reason: SuppressionReason

    def __post_init__(self):
        _require_active(self.attempt)

        if not (type((actual_reason := self.reason)) is (expected_type := SuppressionReason)):
            raise TypeError(
                f"{type(actual_reason)=} ≠ {expected_type=}"
            )




def _document(checkpoint):
    if type(checkpoint) is StartPending:
        return {"kind": "start_pending", "start": json.loads(_encode_pending(checkpoint))}

    if type(checkpoint) is AcknowledgedCheckpoint:
        return {"kind": "acknowledged",
                "start": json.loads(_encode_pending(checkpoint.start)),
                "admission": json.loads(_admission_bytes(checkpoint.admission))}

    if type(checkpoint) is EnteredCheckpoint:
        return {"kind": "entered", "acknowledgement": _document(checkpoint.acknowledgement), "prepared": asdict(checkpoint.prepared)}

    if type(checkpoint) is TerminalSelectedCheckpoint:
        return {"kind": "terminal_selected",
                "attempt": _document(checkpoint.attempt),
                "operation": checkpoint.operation.value,
                "body_sha256": checkpoint.body_sha256,
                "body_bytes": checkpoint.body_bytes}

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
