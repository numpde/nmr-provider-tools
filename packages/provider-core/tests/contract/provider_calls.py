"""Authored API calls whose public wire facts are checked against the release."""

from dataclasses import dataclass

from provider_core.attempt_request import complete_body, fail_body, progress_body, start_body
from provider_core.hello import hello_body
from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response, observation, progress_response, start_response
from tests.fakes.hello import announcement, receipt
from tests.fakes.job_feed import response as jobs_response
from tests.fakes.job_input import facts
from tests.fakes.job_input import response as input_response
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, membership


@dataclass(frozen=True)
class ProviderCall:
    method_name: str
    arguments: tuple
    options: dict
    response: bytes
    body: bytes


def provider_calls():
    """Keep all eleven operations and their exact request-body handoffs explicit."""
    hello = announcement()
    hello_request = hello_body(**{name: value for name, value in hello.items() if name != "provider_ref"})
    start = start_body("job:example", "retained-start:01")
    complete = complete_body(ATTEMPT_REF, " result\n雪 ", b"result bytes")
    failed = fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 ")
    return {
        "provider_hello": ProviderCall("publish_hello", (), hello, receipt(), hello_request),
        "jobs_list": ProviderCall(
            "jobs",
            (),
            {"analysis_kind_ref": "nmr_predict", "has_provider_execution_attempt": False, "limit": 2, "cursor": None},
            jobs_response(),
            b"",
        ),
        "execution_attempts_list": ProviderCall(
            "execution_attempts",
            (),
            {"state": "in_progress", "limit": 2, "cursor": None},
            inventory_response(),
            b"",
        ),
        "job_input_read": ProviderCall("job_input", (facts(),), {"analysis_kind_ref": "nmr_predict"}, input_response(), b""),
        "job_upload_set_read": ProviderCall("uploads", ("job:example",), {}, encoded(membership()), b""),
        "job_upload_read_capability": ProviderCall("read_grant", ("job:example", UPLOAD_REF), {}, encoded(grant()), b""),
        "execution_attempt_read": ProviderCall("read_attempt", (ATTEMPT_REF,), {"job_ref": "job:example"}, observation(), b""),
        "execution_attempt_start": ProviderCall(
            "start_attempt",
            (start,),
            {"job_ref": "job:example", "analysis_kind_ref": "nmr_predict", "provider_ref": "provider:example"},
            start_response(),
            start,
        ),
        "execution_attempt_progress": ProviderCall(
            "progress_attempt",
            (ATTEMPT_REF,),
            {"phase": "running", "condition_code": None},
            progress_response(phase="running"),
            progress_body("running", None),
        ),
        "execution_attempt_complete": ProviderCall(
            "complete_attempt",
            (complete,),
            {"execution_attempt_ref": ATTEMPT_REF},
            complete_response(),
            complete,
        ),
        "execution_attempt_fail": ProviderCall(
            "fail_attempt",
            (failed,),
            {"execution_attempt_ref": ATTEMPT_REF},
            fail_response(),
            failed,
        ),
    }
