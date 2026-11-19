"""Interpret admitted refusals without changing an operation's send history.

Authority: the selected API's security/problems.py and operations/errors.py.
Grant-specific conflicts precede issuance in provider_job_upload_read_capability.py.
Each predicate describes this send only, never an earlier unanswered request.
The caller owns correlation/admission and retains uncertainty when no fact is known.
"""

from provider_core.container_guard import require_container

require_container()


def _no_domain_change(status: int | None, problem_code: str | None) -> bool:
    return (
        (problem_code is not None) and
        ((status == 401) or
         (status == 408 and problem_code == "request_body_timeout"))
    )


def issuance_refused(status: int | None, problem_code: str | None) -> bool:
    """Report whether an admitted refusal establishes that this send issued no link."""

    return (
        _no_domain_change(status, problem_code) or
        (status == 409 and problem_code in {"upload_read_not_finalized", "project_purge_in_progress"})
    )


def attempt_change_refused(status: int | None, problem_code: str | None) -> bool:
    """Report whether this send changed neither Attempt admission nor its outcome."""

    return (
        _no_domain_change(status, problem_code) or
        ((status == 409) and
         (problem_code in {
             "provider_attempt_key_conflict",
             "job_provider_attempt_limit_reached",
             "job_attempt_limit_reached",
             "execution_attempt_progress_terminal",
             "execution_attempt_progress_regression",
             "execution_attempt_outcome_expired",
             "execution_attempt_completion_after_failure",
             "execution_attempt_failure_after_success",
             "execution_attempt_completion_replay_mismatch",
             "execution_attempt_failure_replay_mismatch",
         }))
    )
