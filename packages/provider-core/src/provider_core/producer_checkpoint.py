"""Compact producer decisions, never scientific payloads or publication receipts.

NMRNet d09b77b docs/notes/021_compose_runner_architecture_20261023.html
supersedes its note017's full suppressed-result retention. The exact small
START and API admission codecs come from attempt_record. Restored metadata
cannot recreate a live session.
Selected-command identity is durable before enqueue; accepted-delivery identity
records a later real handoff and does not claim API publication or native exit.
"""

from provider_core.container_guard import require_container

require_container()

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from provider_core.attempt_record import (
    MAX_PENDING_RECORD_BYTES, ProtectedEvidence, RecoveryScope, StartPending, SuppressionReason, TerminalOperation,
    _admission_bytes, _decode_pending, _encode_pending,
)
from provider_core.attempt_request import _scalar_text
from provider_core.attempt_response import ATTEMPT_RESPONSE_BYTES, AttemptAdmission, read_start
from provider_core.response_json import read_document

# The existing START/admission bounds plus a 1024-scalar schema identifier,
# digest/size and fixed state labels; there is no future result-body reservation.
# Bound the compact durable checkpoint envelope independently of scientific payload size.
# repo-check: allow-module-constant
MAX_CHECKPOINT_BYTES = MAX_PENDING_RECORD_BYTES + ATTEMPT_RESPONSE_BYTES + 16384
# Persisted compact checkpoints use this exact versioned codec identity.
# repo-check: allow-module-constant
SCHEMA = "provider_core.producer_checkpoint.v1"


# silent-defaults: allow-contract -- Production retains its existing shared volume names; a contained recipe may supply its admitted private project.
def checkpoint_volume_name(scope, *, namespace="nmr-api-delivery"):
    """Keep producer control private to its original API/provider/lane/generation."""
    if type(scope) is not RecoveryScope:
        raise ValueError("Checkpoint volume naming requires the admitted recovery scope.")
    raw = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":")).encode()
    return namespace + "-control-" + hashlib.sha256(raw).hexdigest()


def _require_digest(value):
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("A control checkpoint digest must be a canonical SHA-256 identity.")


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    schema_id: str
    sha256: str
    byte_length: int

    def __post_init__(self):
        _scalar_text(self.schema_id, "prepared identity schema")
        _require_digest(self.sha256)
        if not (type(self.byte_length) is int and 1 <= self.byte_length):
            raise ValueError("A prepared identity digest requires its positive byte length.")

    @classmethod
    def from_evidence(cls, evidence):
        if type(evidence) is not ProtectedEvidence:
            raise ValueError("Digest the actual protected prepared identity before retaining control metadata.")
        return cls(evidence.schema_id, "sha256:" + hashlib.sha256(evidence.content).hexdigest(), len(evidence.content))


@dataclass(frozen=True, slots=True)
class AcknowledgedCheckpoint:
    """Historical scoped START admission; no live execution handle is restored."""

    start: StartPending
    admission: AttemptAdmission

    def __post_init__(self):
        if type(self.start) is not StartPending:
            raise ValueError("An acknowledgement checkpoint requires its exact pending START.")
        read_start(_admission_bytes(self.admission), job_ref=self.start.input.job_ref,
                   analysis_kind_ref=self.start.analysis_kind_ref, provider_ref=self.start.scope.provider_ref)


@dataclass(frozen=True, slots=True)
class EnteredCheckpoint:
    acknowledgement: AcknowledgedCheckpoint
    prepared: EvidenceDigest

    def __post_init__(self):
        if (type(self.acknowledgement) is not AcknowledgedCheckpoint
                or self.acknowledgement.admission.state != "in_progress" or type(self.prepared) is not EvidenceDigest):
            raise ValueError("Execution entry requires an active acknowledgement and only its prepared identity digest.")


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
    if type(checkpoint) not in (AcknowledgedCheckpoint, EnteredCheckpoint):
        raise ValueError("A terminal control decision requires an acknowledged or entered checkpoint.")
    if checkpoint_acknowledgement(checkpoint).admission.state != "in_progress":
        raise ValueError("A terminal START replay grants no execution or fresh terminal selection.")


@dataclass(frozen=True, slots=True)
class TerminalSelectedCheckpoint:
    """An approved immutable command's identity before any enqueue may occur."""

    attempt: AcknowledgedCheckpoint | EnteredCheckpoint
    operation: TerminalOperation
    body_sha256: str
    body_bytes: int

    def __post_init__(self):
        _require_active(self.attempt)
        if type(self.operation) is not TerminalOperation:
            raise ValueError("Terminal selection requires an explicit complete or fail operation.")
        _require_digest(self.body_sha256)
        if not (type(self.body_bytes) is int and 1 <= self.body_bytes):
            raise ValueError("Terminal selection requires the chosen command's positive byte length.")


@dataclass(frozen=True, slots=True)
class DeliveryAcceptedCheckpoint:
    """Historical durable relay handoff; not an API receipt or cleanup proof."""

    selection: TerminalSelectedCheckpoint

    def __post_init__(self):
        if type(self.selection) is not TerminalSelectedCheckpoint:
            raise ValueError("Delivery acceptance requires the original selected command identity.")


@dataclass(frozen=True, slots=True)
class SuppressedCheckpoint:
    """Retain why a candidate was not selected, without persisting that candidate."""

    attempt: AcknowledgedCheckpoint | EnteredCheckpoint
    reason: SuppressionReason

    def __post_init__(self):
        _require_active(self.attempt)
        if type(self.reason) is not SuppressionReason:
            raise ValueError("Suppression requires the owned disposition, without scientific payloads.")


def checkpoint_start(checkpoint):
    return checkpoint if type(checkpoint) is StartPending else checkpoint_acknowledgement(checkpoint).start


def _document(checkpoint):
    if type(checkpoint) is StartPending:
        return {"kind": "start_pending", "start": json.loads(_encode_pending(checkpoint))}
    if type(checkpoint) is AcknowledgedCheckpoint:
        return {"kind": "acknowledged", "start": json.loads(_encode_pending(checkpoint.start)),
                "admission": json.loads(_admission_bytes(checkpoint.admission))}
    if type(checkpoint) is EnteredCheckpoint:
        return {"kind": "entered", "acknowledgement": _document(checkpoint.acknowledgement), "prepared": asdict(checkpoint.prepared)}
    if type(checkpoint) is TerminalSelectedCheckpoint:
        return {"kind": "terminal_selected", "attempt": _document(checkpoint.attempt), "operation": checkpoint.operation.value,
                "body_sha256": checkpoint.body_sha256, "body_bytes": checkpoint.body_bytes}
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


def _fields(document, fields):
    if type(document) is not dict or set(document) != fields:
        raise ValueError("Cannot restore producer control checkpoint: fields do not match its state.")


def _acknowledged(document, scope):
    _fields(document, {"kind", "start", "admission"})
    if document["kind"] != "acknowledged":
        raise ValueError("Cannot restore producer checkpoint: expected acknowledgement metadata.")
    # Match the START encoder: separator whitespace must not consume its budget.
    start = _decode_pending(json.dumps(document["start"], separators=(",", ":")).encode(), scope=scope)
    admission = read_start(json.dumps(document["admission"]).encode(), job_ref=start.input.job_ref,
                           analysis_kind_ref=start.analysis_kind_ref, provider_ref=start.scope.provider_ref)
    return AcknowledgedCheckpoint(start, admission)


def _active(document, scope):
    if type(document) is dict and document.get("kind") == "acknowledged":
        return _acknowledged(document, scope)
    _fields(document, {"kind", "acknowledgement", "prepared"})
    if document["kind"] != "entered":
        raise ValueError("Cannot restore producer checkpoint: expected active control metadata.")
    _fields(document["prepared"], {"schema_id", "sha256", "byte_length"})
    return EnteredCheckpoint(_acknowledged(document["acknowledgement"], scope), EvidenceDigest(**document["prepared"]))


def _selected(document, scope):
    _fields(document, {"kind", "attempt", "operation", "body_sha256", "body_bytes"})
    if document["kind"] != "terminal_selected":
        raise ValueError("Cannot restore producer checkpoint: expected selected command identity.")
    try:
        operation = TerminalOperation(document["operation"])
    except ValueError:
        raise ValueError("Cannot restore producer checkpoint: terminal operation is unsupported.") from None
    return TerminalSelectedCheckpoint(_active(document["attempt"], scope), operation, document["body_sha256"], document["body_bytes"])


def decode_checkpoint(raw, *, scope):
    document = read_document(raw, MAX_CHECKPOINT_BYTES)
    if type(document) is not dict or document.get("schema_id") != SCHEMA:
        raise ValueError("Cannot restore producer checkpoint: schema is unsupported.")
    state = {name: value for name, value in document.items() if name != "schema_id"}
    kind = state.get("kind")
    if kind == "start_pending":
        _fields(state, {"kind", "start"})
        return _decode_pending(json.dumps(state["start"], separators=(",", ":")).encode(), scope=scope)
    if kind in ("acknowledged", "entered"):
        return _active(state, scope)
    if kind == "terminal_selected":
        return _selected(state, scope)
    if kind == "delivery_accepted":
        _fields(state, {"kind", "selection"})
        return DeliveryAcceptedCheckpoint(_selected(state["selection"], scope))
    if kind == "suppressed":
        _fields(state, {"kind", "attempt", "reason"})
        try:
            reason = SuppressionReason(state["reason"])
        except ValueError:
            raise ValueError("Cannot restore producer checkpoint: suppression disposition is unsupported.") from None
        return SuppressedCheckpoint(_active(state["attempt"], scope), reason)
    raise ValueError("Cannot restore producer checkpoint: state is unsupported.")
