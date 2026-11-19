"""Live START/admission values and the compact checkpoint's exact START codec.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal.py::StartPending supplies the exact START and
generation-sensitive scope precedent. Consumer-lane identity separates async
owners. This codec neither persists records nor claims work.
"""

from provider_core.container_guard import require_container

require_container()

import json
from base64 import b64encode
from dataclasses import asdict, dataclass, field, replace
from enum import Enum

from provider_core.adapters.nmr.v1.attempt_response import start_admission_body

from provider_core.validation.upstream.nmr.v1.attempt_request import (
    require_analysis_kind,
    require_provider_ref,
    require_start_body,
)

from provider_core.validation.upstream.nmr.v1.attempt_response import (
    AttemptAdmission,
    attempt_is_in_progress,
    read_start,
)

from provider_core.validation.upstream.nmr.v1.job_input import FeedInputFacts
from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.signing import https_origin
from provider_core.validation.evidence import require_evidence_schema_id
from provider_core.type_requirements import require_type

# Pending starts only: 4096 request bytes become at most 5464 base64 characters;
# two scope identities of 1024 scalars each need at most 24576 JSON escape bytes.
# The encoder checks total size, including the API origin and metadata.
# Bound the exact-request envelope admitted into live control bookkeeping.
# repo-check: allow-module-constant
MAX_PENDING_RECORD_BYTES = 65536

# Diagnostic envelope precedent: pinned NMRPeak attempt_diagnostic.py. Unlike its
# history trimming, required evidence here is rejected rather than truncated.
# Bound the protected retained evidence envelope.
# repo-check: allow-module-constant
MAX_EVIDENCE_BYTES = 262144


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryScope:
    """An explicit recovery binding, not authentication or an ownership lock."""

    api_origin: str
    provider_ref: str
    consumer_id: str
    generation_id: str

    def __post_init__(self):
        (origin, _authority) = https_origin(self.api_origin)

        if origin != self.api_origin:
            raise ValueError("Cannot bind recovery scope: API origin must be canonical HTTPS")

        require_provider_ref(self.provider_ref)

        for value in (self.consumer_id, self.generation_id):
            require_type(value, str)

            if (not (1 <= len(value) <= 1024)) or (any(((c == "\x00") or (0xD800 <= ord(c) <= 0xDFFF)) for c in value)):
                raise ValueError("Cannot bind recovery scope: consumer and generation identities require 1 through 1024 Unicode scalar values without NUL")


@dataclass(frozen=True, slots=True)
class StartPending:
    """A retained start obligation; dispatch may or may not already have occurred."""

    scope: RecoveryScope = field(repr=False)
    analysis_kind_ref: str
    input: FeedInputFacts
    body: bytes = field(repr=False)

    def __post_init__(self):
        require_type(self.scope, RecoveryScope)

        require_type(self.input, FeedInputFacts)

        require_analysis_kind(self.analysis_kind_ref)
        require_start_body(self.body, job_ref=self.input.job_ref)


class LocalExecutionPhase(Enum):
    NOT_ENTERED = "not_entered"
    EXECUTION_ENTERED = "execution_entered"


@dataclass(frozen=True, slots=True)
class AcknowledgedAttempt:
    """Historical server admission and local entry; neither recreates a live handle.

    Pinned NMRPeak attempt_journal.py::bind_started_attempt/mark_execution_entered
    establishes the entry boundary. Unlike its active-only record, this value also
    retains terminal start replays without granting permission to execute them.
    Live preparation stays in memory; entry retains its binding-owned identity.
    """

    start: StartPending
    admission: AttemptAdmission
    local_phase: LocalExecutionPhase
    prepared_identity: "ProtectedEvidence | None" = field(default=None, repr=False)

    def __post_init__(self):
        require_type(self.start, StartPending)

        require_type(self.local_phase, LocalExecutionPhase)

        if self.prepared_identity is not None:
            actual_prepared_identity = self.prepared_identity

            require_type(actual_prepared_identity, ProtectedEvidence)

            if not (self.local_phase is LocalExecutionPhase.EXECUTION_ENTERED):
                raise ValueError("Cannot retain prepared identity: protected evidence belongs to the execution-entry transition.")

        try:
            read_start(
                start_admission_body(self.admission),
                job_ref=self.start.input.job_ref,
                analysis_kind_ref=self.start.analysis_kind_ref,
                provider_ref=self.start.scope.provider_ref,
            )
        except ResponseRejected as cause:
            raise ValueError(f"Cannot retain acknowledgement: {cause = }") from None

        if (self.local_phase is LocalExecutionPhase.EXECUTION_ENTERED) and (not attempt_is_in_progress(self.admission)):
            raise ValueError("Cannot record execution entry from a terminal start acknowledgement")


class TerminalOperation(Enum):
    COMPLETE = "complete"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ProtectedEvidence:
    """Required binding-owned diagnostics, retained exactly and never rendered here.

    The binding owns schema meaning, secret removal and necessary causal/retirement
    observations. These bytes are evidence, never a resource-release grant.
    """

    schema_id: str = field(repr=False)
    content: bytes = field(repr=False)

    def __post_init__(self):
        require_evidence_schema_id(self.schema_id)

        require_type(self.content, bytes)

        if not (1 <= len(self.content) <= MAX_EVIDENCE_BYTES):
            raise ValueError("Cannot retain required evidence: immutable content must contain 1 through 262144 bytes; evidence is never truncated")


class SuppressionReason(Enum):
    SERVER_STOP = "server_stop"
    OBSERVATION_FAILED = "observation_failed"
    CALLER_CANCELLED = "caller_cancelled"


def acknowledge_start(start, admission):
    return AcknowledgedAttempt(start, admission, LocalExecutionPhase.NOT_ENTERED)


def mark_execution_entered(record, prepared_identity):
    """Record intent before invoking a prepared handle; never infer actual execution."""

    require_type(record, AcknowledgedAttempt)

    if not (record.local_phase is LocalExecutionPhase.NOT_ENTERED):
        raise ValueError("Cannot enter execution: an acknowledged Attempt that has not entered is required")

    require_type(prepared_identity, ProtectedEvidence)

    return replace(record, local_phase=LocalExecutionPhase.EXECUTION_ENTERED, prepared_identity=prepared_identity)


def _encode_pending(record):
    raw = (
        json
        .dumps(
            {
                "schema_id": "provider_core.start_pending.v1",
                "scope": asdict(record.scope),
                "analysis_kind_ref": record.analysis_kind_ref,
                "input": asdict(record.input),
                "start_body": b64encode(record.body).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        .encode("utf-8")
    )

    pending_start_bytes = len(raw)

    if not (pending_start_bytes <= MAX_PENDING_RECORD_BYTES):
        raise ValueError(f"{pending_start_bytes=} should be at most {MAX_PENDING_RECORD_BYTES=}")

    return raw
