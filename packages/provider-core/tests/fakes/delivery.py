"""Small exact terminal requests shared by delivery queue checks."""

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import fail_body
from provider_core.delivery_store import DeliveryOutcome
from tests.fakes.attempts import ATTEMPT_REF


def healthy_diagnostics():
    """No local output errors observed by fixtures that do not emit logs."""
    return {"output_failures": 0, "last_failure_type": None, "last_failure_errno": None, "last_failed_event": None}


# silent-defaults: allow-contract -- The fixed provider, Attempt suffix and payload sentinel prove exact retention and disclosure behavior.
def outcome(*, provider="provider:test", suffix="0", message="SCIENTIFIC_PAYLOAD_NEEDLE"):
    reference = ATTEMPT_REF[:-1] + suffix
    return DeliveryOutcome(
        "https://api.example", provider, "job:queue", reference,
        TerminalOperation.FAIL, fail_body(reference, "provider_interrupted", message) + b"\n",
    )
