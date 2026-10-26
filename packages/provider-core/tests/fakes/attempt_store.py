"""Synthetic START, admission and scope values for checkpoint/lifecycle tests."""

from provider_core.attempt_record import ProtectedEvidence, RecoveryScope, StartPending
from provider_core.attempt_response import read_start
from tests.fakes.attempts import retained_start, start_response
from tests.fakes.job_input import facts

PREPARED_IDENTITY = ProtectedEvidence("fixture.prepared_input.v1", b"scripted admitted candidate")


def scope():
    return RecoveryScope("https://api.example", "provider:example", "lane:test", "generation:one")


# silent-defaults: allow-contract -- The specimen has one fixed retained-record identity, overridden explicitly by identity tests.
def record(key="retained:one"):
    return StartPending(scope(), "nmr_predict", facts(), retained_start(provider_attempt_key=key))


def admission(**changes):
    return read_start(
        start_response(**changes), job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
    )
