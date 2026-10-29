"""Protected projections for exact core-owned failures, without graph traversal.

Ports the core branches of nmrnet-repro37cf6a15cdd58de7bd2ec0a02b33442a2c14f032,
nmrnet_provider/process_reports.py::_owned_relationships/_safe_owner_details
(SHA-256 ee13ebc042ba407d957608477f3343a084cc7ce0143f84ed6971371468e25f7b).
The consumer still owns standard cause/context/groups, cycle and display bounds,
credential redaction and its recipient policy. Returned facts are PROTECTED and
unredacted; borrowed exceptions may contain secrets or private data. This module
never renders raw response bodies, arbitrary exception text or scientific output.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.api import ApiFailure
from provider_core.analysis import ObservedWorkerCrash
from provider_core.api_config import ApiConfigurationRejected
from provider_core.attempt_record import AcknowledgedAttempt
from provider_core.chat import ChatFailed
from provider_core.controller import ControllerFailed, ControllerRefused
from provider_core.conversation import ConversationFailed, ConversationRetirementFailed, ToolRejected
from provider_core.credential import CredentialRejected
from provider_core.delivery_client import DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_config import DeliveryConfigurationRejected
from provider_core.delivery_ipc import DeliveryRequestRejected
from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryInventoryUnavailable, DeliveryStorageFailed
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.execution import (
    ConditionRecoveryExpired,
    ExecutionFailed,
    ExecutionRefused,
    ExecutionStopped,
    ExecutionStopRequested,
    ObservationGapExpired,
)
from provider_core.ownership import LiveCapacityFull, OwnershipRefused
from provider_core.lifetime import LifetimeRefused
from provider_core.runner_protocol import LifecycleRejected, RetirementUnconfirmed
from provider_core.starting import StartFailed, StartRefused
from provider_core.startup_file import StartupFileRejected
from provider_core.transport import ExchangeFailed, response_request_id
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected


def owned_relationships(error):
    """Yield labelled original owner references, including absent/repeated causes.

    Unknown types and subclasses expose nothing. The caller decides how to walk,
    redact and bound this protected graph; ordinary Python chaining is not copied.
    """
    kind = type(error)
    if kind is ConversationRetirementFailed:
        for endpoint_index, cause in error.failures:
            yield f"interpreter endpoint {endpoint_index} cleanup", cause
    if kind in (ConversationFailed, ConversationRetirementFailed):
        for endpoint_index, cause in error.endpoint_failures:
            yield f"interpreter endpoint {endpoint_index} failure", cause
        for turn_index, cause in error.correction_failures:
            yield f"interpreter turn {turn_index} correction failure", cause
    if kind in (
        CredentialRejected,
        ObservedWorkerCrash,
        ApiFailure,
        ControllerFailed,
        ExecutionFailed,
        ObservationGapExpired,
        ConditionRecoveryExpired,
        StartFailed,
        ChatFailed,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
        ConversationFailed,
        ConversationRetirementFailed,
        DeliveryUnavailable,
        DeliveryStorageFailed,
        DeliveryWorkerFailed,
        StartupFileRejected,
        ApiConfigurationRejected,
        DeliveryConfigurationRejected,
    ):
        yield "owned cause", error.cause
    if kind in (
        ControllerFailed,
        ExecutionFailed,
        ExchangeFailed,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
        DeliveryWorkerFailed,
        StartupFileRejected,
    ):
        for cause in error.cleanup_failures:
            yield "independent cleanup", cause
    if kind in (ConversationRetirementFailed, ChatFailed, ExecutionFailed, ControllerFailed):
        yield "interruption cause", error.interruption_cause
    if kind is ExecutionFailed:
        yield "local delivery failure", error.delivery_failure
        if error.delivery_diagnostic_failure is not None:
            yield "delivery retry diagnostic", error.delivery_diagnostic_failure
        yield "handoff interruption", error.handoff_interruption
        yield "analysis operation interruption", error.operation_interruption
        yield "first condition failure", error.condition_failure
        yield "latest condition failure", error.latest_condition_failure
    if kind is StartFailed:
        for cause in error.settlement_failures:
            yield "independent settlement", cause
    if kind in (ExecutionFailed, StartFailed, DeliveryWorkerFailed):
        yield "API failure", error.api_failure
    if kind is ExchangeFailed:
        yield "transport primary", error.primary
        yield "transport interruption", error.interruption
    if kind in (UploadFailed, UploadInputRejected):
        yield "acquisition interruption", error.interruption
    if kind is UploadFileReadFailed:
        yield "source reader interruption", error.interruption


def owned_details(error):
    """Return fresh scalar facts for protected, redacted consumer reporting.

    Only exact reviewed owner types may provide text/fields. Received metadata
    says nothing about durable acceptance; transport correlation keeps its own
    header admission. Redaction and display truncation remain with the consumer.
    """
    kind = type(error)
    if kind not in (
        CredentialRejected,
        ObservedWorkerCrash,
        ApiFailure,
        ControllerFailed,
        ControllerRefused,
        ExecutionFailed,
        ExecutionRefused,
        ExecutionStopped,
        ExecutionStopRequested,
        ObservationGapExpired,
        ConditionRecoveryExpired,
        OwnershipRefused,
        LiveCapacityFull,
        LifetimeRefused,
        StartFailed,
        StartRefused,
        ChatFailed,
        ConversationFailed,
        ConversationRetirementFailed,
        ToolRejected,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
        ExchangeFailed,
        LifecycleRejected,
        RetirementUnconfirmed,
        DeliveryRejected,
        DeliveryRequestRejected,
        DeliveryUnavailable,
        DeliveryConflict,
        DeliveryFull,
        DeliveryInventoryUnavailable,
        DeliveryStorageFailed,
        DeliveryWorkerFailed,
        StartupFileRejected,
        ApiConfigurationRejected,
        DeliveryConfigurationRejected,
    ):
        return {}
    details = {"message": str(error)}
    if kind is ObservedWorkerCrash:
        details.update({
            "phase": error.phase, "returncode": error.native_exit.returncode,
            "worker_role": error.native_exit.role, "worker_generation": error.native_exit.generation,
            "worker_operation": error.native_exit.operation,
        })
    if kind is DeliveryUnavailable:
        details["status"] = error.status
    if kind is DeliveryWorkerFailed:
        details["api_receipt_received"] = error.api_receipt_received
    if kind is LifecycleRejected:
        if error.returncode is not None:
            details["returncode"] = error.returncode
        if error.scratch_released is not None:
            details["scratch_released"] = error.scratch_released
    if kind is ApiFailure:
        details.update(
            operation=error.operation,
            status=error.status,
            request_id=error.request_id,
            problem_code=error.problem_code,
            issuance_may_have_committed=error.issuance_may_have_committed,
            attempt_mutation_may_have_committed=error.attempt_mutation_may_have_committed,
        )
    if kind is ExecutionFailed and type(error.attempt) is AcknowledgedAttempt:
        details.update(
            job_ref=error.attempt.admission.job_ref, execution_attempt_ref=error.attempt.admission.execution_attempt_ref,
            delivery_acceptance_received=error.delivery_acceptance is not None,
        )
    if kind is ObservationGapExpired:
        details.update(
            gap_seconds=error.gap_seconds, read_started=error.read_started, observation_pending=error.observation_pending
        )
    if kind is ConditionRecoveryExpired:
        details.update(window_seconds=error.window_seconds, phase=error.phase, condition_code=error.condition_code)
    if kind is ExecutionFailed:
        details["outcome_in_memory"] = error.outcome is not None
        details["condition_receipt_received"] = error.condition_receipt is not None
    if kind in (UploadFailed, UploadInputRejected):
        details["grant_issued"] = error.grant_issued
        if error.response is not None:
            details.update(status=error.response.status, request_id=response_request_id(error.response.headers))
    if kind is ChatFailed:
        details["reason"] = error.reason
        if error.endpoint_error is not None:
            details["endpoint_error_unverified"] = True
            for name in ("message", "type", "code", "param", "detail_unavailable"):
                value = error.endpoint_error.get(name)
                if type(value) is str:
                    details["endpoint_error_" + name] = value
        head = error.response
        if head is None and type(error.cause) is ExchangeFailed:
            head = error.cause.response_head
        if head is not None:
            details.update(status=head.status, request_id=response_request_id(head.headers))
    if kind is ExchangeFailed and error.response_head is not None:
        details.update(status=error.response_head.status, request_id=response_request_id(error.response_head.headers))
    if kind is ExchangeFailed and error.proxy_connect_status is not None:
        details["proxy_connect_status"] = error.proxy_connect_status
    if kind is StartFailed:
        details["acknowledgement_received"] = error.acknowledgement is not None
        if error.acknowledgement is not None:
            details["observed_attempt_state"] = error.acknowledgement.admission.state
    return details
