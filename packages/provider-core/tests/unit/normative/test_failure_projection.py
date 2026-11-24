"""Core projects owned facts; consumers retain redaction and graph traversal."""

import asyncio
import json
import unittest
import os

from provider_core.api_errors import ApiFailure
from provider_core.attempt_record import ProtectedEvidence, acknowledge_start
from provider_core.analysis import AnalysisSuccess
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.decoded_body import DecodedBodyRejected
from provider_core.deployment_binding import BindingDefinitionRejected
from provider_core.deployment_route import RouteProjectionRejected
from provider_core.chat import ChatFailed
from provider_core.conversation import ConversationRetirementFailed, ToolRejected
from provider_core.execution import ExecutionFailed
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.diagnostics import failure_evidence
from provider_core.payload import PayloadRejected
from provider_core.process_wire import MAX_TRACEBACK_FRAMES, RemoteScienceError, decode_frame, frame
from provider_core.starting import StartFailed
from provider_core.transport import ExchangeFailed, HttpResponse, HttpResponseHead
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected, UploadRecoveryExhausted
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF


class CoreFailureProjectionTests(unittest.TestCase):
    def test_local_payload_rejection_retains_reason_and_redacts_credentials(self):
        error = PayloadRejected("Missing formula; token=private-token", code=PayloadRejected.Code.INVALID_VALUE)
        projected = failure_evidence(error, secrets=())
        self.assertIn("Missing formula", projected["message"])
        self.assertEqual(error.code.value, projected["code"])
        self.assertNotIn("private-token", json.dumps(projected))

    def test_invalid_utf8_retains_location_without_source_bytes(self):
        try:
            b"private-input-\xff".decode("utf-8")
        except UnicodeDecodeError as error:
            projected = failure_evidence(error, secrets=())

        self.assertEqual(14, projected["start"])
        self.assertEqual("utf-8", projected["encoding"])
        self.assertIn("reason", projected)
        self.assertNotIn("private-input", json.dumps(projected))

    def test_decoded_child_failure_reaches_protected_evidence_with_redacted_locations(self):
        secret = "child-private-canary"
        child_error_type = type("ChildSpectrumFailure", (Exception,), {"__module__": "science." + secret})

        def child_spectrum_calculation():
            _local_payload = "unserialized-scientific-payload"
            _live_model = object()
            raise child_error_type("Spectrum convergence failed with " + secret)

        filename = "/science/child-" + secret + ".py"
        child_spectrum_calculation.__code__ = child_spectrum_calculation.__code__.replace(co_filename=filename)

        try:
            child_spectrum_calculation()
        except child_error_type as child_failure:
            location = child_failure.__traceback__

            while location.tb_next is not None:
                location = location.tb_next

            expected_line = location.tb_lineno
            operation_failure = RuntimeError("Scientific operation failed")
            operation_failure.__cause__ = child_failure
            received = decode_frame(frame(ExceptionGroup("Scientific failures", [operation_failure]))[4:])

        received_child = received.exceptions[0].__cause__
        received_child.remote_frames = [{"file": "FORGED_MUTABLE_FRAME"}]
        evidence = failure_evidence(received, secrets=(secret,))
        child_evidence = evidence["relationships"][0]["failure"]["relationships"][0]["failure"]
        rendered = json.dumps(evidence)

        self.assertIs(type(received_child), RemoteScienceError)
        self.assertEqual(child_evidence["remote_type"], "science.[redacted].ChildSpectrumFailure")
        self.assertEqual(child_evidence["message"], "Spectrum convergence failed with [redacted]")

        self.assertEqual(
            child_evidence["remote_frames"][-1],
            {"file": "/science/child-[redacted].py", "function": "child_spectrum_calculation", "line": expected_line},
        )

        self.assertFalse(child_evidence["remote_frames_omitted"])
        self.assertNotIn(secret, rendered)
        self.assertNotIn("unserialized-scientific-payload", rendered)
        self.assertNotIn("_live_model", rendered)
        self.assertNotIn("FORGED", rendered)

    def test_decoded_evidence_ignores_mutable_attributes_and_unreviewed_foreign_properties(self):
        class HostileEvidence:
            @property
            def message(self):
                raise AssertionError("Unreviewed evidence property was executed")

            def __str__(self):
                raise AssertionError("Unreviewed exception text was rendered")

        class ForeignFailure(RemoteScienceError):
            @property
            def _process_error_evidence(self):
                raise AssertionError("Foreign provenance property was executed")

            @property
            def remote_frames(self):
                raise AssertionError("Foreign frame property was executed")

            def __str__(self):
                raise AssertionError("Foreign exception text was rendered")

        received = decode_frame(frame(LookupError("Decoder-owned spectrum failure"))[4:])
        received.remote_type = "FORGED_REMOTE_TYPE"
        received.remote_frames = HostileEvidence()
        received.remote_frames_omitted = True
        received.__notes__ = ["FORGED_NOTE"]
        received.args = (HostileEvidence(),)
        evidence = failure_evidence(received, secrets=())

        self.assertEqual(evidence["message"], "Decoder-owned spectrum failure")
        self.assertEqual(evidence["remote_type"], "builtins.LookupError")
        self.assertEqual(evidence["remote_frames"], [])
        self.assertFalse(evidence["remote_frames_omitted"])
        self.assertNotIn("FORGED", repr(evidence))

        forged = RuntimeError("UNREVIEWED_MESSAGE")
        forged._process_error_evidence = HostileEvidence()

        for failure in (forged, ForeignFailure(), RemoteScienceError("UNREVIEWED_MESSAGE")):
            with self.subTest(kind=type(failure)):
                evidence = failure_evidence(failure, secrets=())

                self.assertNotIn("message", evidence)
                self.assertNotIn("remote_type", evidence)
                self.assertNotIn("remote_frames", evidence)

    def test_decoded_protected_evidence_redacts_before_its_field_limits(self):
        secret = "protected-boundary-canary"
        wire_failure = json.loads(frame(RuntimeError("Child failure"))[4:])
        wire_failure["original_type"] = "t" * 120 + secret + "ChildFailure"
        wire_failure["message"] = "Spectrum failed: " + "m" * 1000 + secret + " trailing explanation"
        wire_failure["frames"] = [
            {"file": "f" * 120 + secret + ".py", "function": "g" * 120 + secret, "line": 12}
            for _ in range(MAX_TRACEBACK_FRAMES)
        ]
        wire_failure["frames_omitted"] = True
        received = decode_frame(json.dumps(wire_failure).encode("ascii"))
        evidence = failure_evidence(received, secrets=(secret,))

        self.assertNotIn(secret, json.dumps(evidence))
        self.assertNotIn("protect", json.dumps(evidence))
        self.assertTrue(evidence["message"].startswith("Spectrum failed:"))
        self.assertIn("truncated", evidence["message"])
        self.assertLessEqual(len(evidence["message"]), 1024 + len("[truncated]"))
        self.assertLessEqual(len(evidence["remote_type"]), 128 + len("[truncated]"))
        self.assertEqual(len(evidence["remote_frames"]), MAX_TRACEBACK_FRAMES)
        self.assertTrue(evidence["remote_frames_omitted"])

        for location in evidence["remote_frames"]:
            self.assertEqual(set(location), {"file", "function", "line"})
            self.assertLessEqual(len(location["file"]), 128 + len("[truncated]"))
            self.assertLessEqual(len(location["function"]), 128 + len("[truncated]"))

    def test_wire_omission_prevents_secret_fragments_reaching_protected_evidence(self):
        secret = "wire-private-canary-" + "z" * 2200
        child_error_type = type("ChildSpectrumFailure", (Exception,), {"__module__": "science." + secret})

        def child_opaque_calculation():
            try:
                raise ValueError("Spectrum calibration is missing")
            except ValueError as cause:
                raise child_error_type("Spectrum failed: " + "m" * 900 + secret) from cause

        try:
            child_opaque_calculation()
        except child_error_type as child_failure:
            received = decode_frame(frame(child_failure)[4:])

        evidence = failure_evidence(received, secrets=(secret,))
        rendered = json.dumps(evidence)

        self.assertIn(member="Diagnostic text omitted", container=evidence["message"])
        self.assertIn(member="Diagnostic text omitted", container=evidence["remote_type"])
        self.assertNotIn(member="wire-private-canary", container=rendered)
        self.assertNotIn(member=secret, container=rendered)
        self.assertIn(member="child_opaque_calculation", container=rendered)
        self.assertEqual(
            first="Spectrum calibration is missing",
            second=evidence["relationships"][0]["failure"]["message"],
        )

    def _hostile_observation_failure(self):
        class HostileObservation:
            @property
            def stage(self):
                raise RuntimeError("PRIVATE_PROPERTY_CANARY")

            @property
            def error(self):
                raise RuntimeError("PRIVATE_PROPERTY_CANARY")

        return ExchangeFailed("Observed failure", network_io=HostileObservation())

    def test_unknown_network_observation_cannot_run_properties_during_projection(self):
        failure = self._hostile_observation_failure()

        self.assertEqual(owned_details(failure), {"message": "Observed failure"})

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_unknown_observation_diagnostics_omit_property_text(self):
        failure = self._hostile_observation_failure()

        self.assertNotIn("PRIVATE_PROPERTY_CANARY", repr(failure_evidence(failure, secrets=())))

    def test_shared_deployment_refusals_have_their_safe_default_message(self):
        for kind in (BindingDefinitionRejected, RouteProjectionRejected):
            with self.subTest(kind=kind):
                failure = kind("Selected deployment input was refused")
                failure.__cause__ = ValueError("PRIVATE_PARSE_DETAIL")

                self.assertEqual(owned_details(failure), {"message": str(failure)})

    def test_decoded_rejection_projects_only_the_closed_reason_and_safe_message(self):
        failure = DecodedBodyRejected("invalid_encoding")
        failure.__cause__ = ValueError("PRIVATE_ENCODING_DETAIL")

        self.assertEqual(owned_details(failure), {"message": str(failure), "reason": "invalid_encoding"})

        class Unreviewed(DecodedBodyRejected):
            pass

        self.assertEqual(owned_details(Unreviewed("invalid_encoding")), {})

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_decoded_rejection_projection_omits_native_text(self):
        failure = DecodedBodyRejected("invalid_encoding")
        failure.__cause__ = ValueError("PRIVATE_ENCODING_DETAIL")

        self.assertNotIn("PRIVATE", repr(owned_details(failure)))

    def _api_failure_with_response(self):
        response = HttpResponse(
            503,
            ((b"x-request-id", b"request:received"), (b"authorization", b"PRIVATE_HEADER")),
            b"PRIVATE_BODY",
        )

        failure = ApiFailure("read owned state", "the API could not admit this read", response=response)
        failure.issuance_may_have_committed = True

        return failure

    def test_api_projects_received_correlation_and_distinct_effect_flags(self):
        failure = self._api_failure_with_response()
        details = owned_details(failure)

        self.assertEqual(
            details,
            {
                "message": str(failure),
                "operation": "read owned state",
                "status": 503,
                "request_id": "request:received",
                "problem_code": None,
                "issuance_may_have_committed": True,
                "attempt_mutation_may_have_committed": False,
            },
        )

        details["status"] = 200

        self.assertEqual(owned_details(failure)["status"], 503)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_api_projection_omits_response_payloads(self):
        self.assertNotIn("PRIVATE", repr(owned_details(self._api_failure_with_response())))

    def test_chat_uses_complete_response_before_transport_head_and_reads_late_metadata(self):
        exchange = ExchangeFailed("Response body did not finish", primary=TimeoutError())
        failure = ChatFailed("exchange_failed", "Cannot complete this model exchange", cause=exchange)

        self.assertNotIn("status", owned_details(failure))
        exchange.response_head = HttpResponseHead(429, ((b"x-request-id", b"request:head"), (b"set-cookie", b"PRIVATE")))

        self.assertEqual(owned_details(failure)["status"], 429)
        self.assertEqual(owned_details(failure)["request_id"], "request:head")
        failure.response = HttpResponse(503, ((b"x-request-id", b"request:complete"),), b"PRIVATE_BODY")

        self.assertEqual(owned_details(failure)["status"], 503)
        self.assertEqual(owned_details(failure)["request_id"], "request:complete")
        exchange.response_head = HttpResponseHead(429, ((b"x-request-id", b"one"), (b"x-request-id", b"two")))

        self.assertIsNone(owned_details(exchange)["request_id"])

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_chat_projection_omits_response_payloads(self):
        exchange = ExchangeFailed(
            "Response body did not finish",
            response_head=HttpResponseHead(429, ((b"set-cookie", b"PRIVATE"),)),
        )

        failure = ChatFailed("exchange_failed", "Cannot complete this model exchange", cause=exchange)

        self.assertNotIn("PRIVATE", repr(owned_details(failure)))
        failure.response = HttpResponse(503, (), b"PRIVATE_BODY")

        self.assertNotIn("PRIVATE", repr(owned_details(failure)))

    def test_transport_edges_preserve_original_identity_order_and_independent_cleanup(self):
        (primary, cleanup, interrupted) = (TimeoutError(), OSError(), asyncio.CancelledError())
        failure = ExchangeFailed("Interrupted response", primary=primary, cleanup_failures=(cleanup,), interruption=interrupted)

        self.assertEqual(
            list(owned_relationships(failure)),
            [
                ("independent cleanup", cleanup),
                ("transport primary", primary),
                ("transport interruption", interrupted),
            ],
        )

    def test_conversation_edges_preserve_endpoint_corrections_primary_and_interruption(self):
        (endpoint, cleanup, cause, previous) = (TimeoutError(), OSError(), RuntimeError(), asyncio.CancelledError())
        correction = ToolRejected("Choose an advertised operation")

        failure = ConversationRetirementFailed(
            ((2, cleanup),),
            cause=cause,
            endpoint_failures=((0, endpoint),),
            correction_failures=((1, correction),),
        )

        failure.interruption_cause = previous
        actual_relationships = list(owned_relationships(failure))
        expected_relationships = (
            (cleanup, ("endpoint", "2", "cleanup")),
            (endpoint, ("endpoint", "0", "failure")),
            (correction, ("turn", "1", "correction")),
            (cause, ("owned", "cause")),
            (previous, ("interruption", "cause")),
        )

        self.assertEqual(first=len(expected_relationships), second=len(actual_relationships))

        for ((label, actual_cause), (expected_cause, context)) in zip(actual_relationships, expected_relationships, strict=True):
            self.assertIs(expr1=expected_cause, expr2=actual_cause)

            for term in context:
                self.assertIn(member=term, container=label)

    def test_acquisition_and_reader_failures_keep_separate_grant_and_interruption_facts(self):
        (cause, cleanup, interrupted) = (OSError(), OSError(), asyncio.CancelledError())

        for kind in (UploadFailed, UploadInputRejected, UploadRecoveryExhausted):
            with self.subTest(kind=kind):
                failure = kind(
                    "Source transfer did not finish",
                    cause=cause,
                    cleanup_failures=(cleanup,),
                    interruption=interrupted
                )

                failure.grant_issued = True

                self.assertTrue(owned_details(failure)["grant_issued"])

                self.assertEqual(
                    list(owned_relationships(failure)),
                    [
                        ("owned cause", cause),
                        ("independent cleanup", cleanup),
                        ("acquisition interruption", interrupted),
                    ],
                )

        reader = UploadFileReadFailed(
            "The acquired source changed",
            cause=cause,
            cleanup_failures=(cleanup,),
            interruption=interrupted
        )

        self.assertNotIn("grant_issued", owned_details(reader))

        self.assertEqual(
            list(owned_relationships(reader)),
            [
                ("owned cause", cause),
                ("independent cleanup", cleanup),
                ("source reader interruption", interrupted),
            ],
        )

    def test_received_start_and_terminal_facts_do_not_infer_durability_or_outcome_acceptance(self):
        active = acknowledge_start(record(), admission())

        for acknowledgement in (None, active):
            failure = StartFailed(
                "Start storage is unconfirmed",
                cause=OSError(),
                acknowledgement=acknowledgement,
                api_failure=None,
                settlement_failures=(),
            )

            details = owned_details(failure)

            self.assertEqual(details["acknowledgement_received"], acknowledgement is active)
            self.assertEqual("observed_attempt_state" in details, acknowledgement is active)

        for received in (False, True):
            failure = DeliveryWorkerFailed(cause=OSError(), api_failure=None, cleanup_failures=(), api_receipt_received=received)
            details = owned_details(failure)

            self.assertEqual(details["api_receipt_received"], received)

        execution = self._execution_with_outcome(active)
        details = owned_details(execution)

        self.assertTrue(details["outcome_in_memory"])
        self.assertIs(execution.attempt, active)
        self.assertEqual(details["job_ref"], active.admission.job_ref)
        self.assertEqual(details["execution_attempt_ref"], ATTEMPT_REF)
        self.assertNotIn("local_phase", details)

    def _execution_with_outcome(self, active):
        candidate = AnalysisSuccess("result.v1", b"PRIVATE_RESULT", ProtectedEvidence("evidence.v1", b"PRIVATE_EVIDENCE"))

        return ExecutionFailed(
            cause=OSError(),
            outcome=candidate,
            api_failure=None,
            cleanup_failures=(),
            attempt=active
        )

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_execution_projection_omits_scientific_bytes(self):
        execution = self._execution_with_outcome(acknowledge_start(record(), admission()))
        execution.cause = RuntimeError("private retirement cause")
        execution.cleanup_failures = (OSError(5, "PRIVATE_NATIVE"),)

        self.assertNotIn("PRIVATE", repr(owned_details(execution)))
        self.assertNotIn("private retirement cause", str(execution))
        self.assertNotIn("PRIVATE_NATIVE", repr(failure_evidence(execution, secrets=())))

    def test_execution_distinguishes_relay_acceptance_from_api_receipt_and_preserves_delivery_cause(self):
        active = acknowledge_start(record(), admission())
        (cause, interruption) = (OSError(), asyncio.CancelledError())

        failure = ExecutionFailed(
            cause=cause,
            outcome=None,
            api_failure=None,
            cleanup_failures=(),
            attempt=active
        )

        self.assertFalse(owned_details(failure)["delivery_acceptance_received"])
        failure.delivery_acceptance = {"body_sha256": "sha256:" + "b" * 64}
        (failure.delivery_failure, failure.handoff_interruption) = (cause, interruption)
        details = owned_details(failure)

        self.assertTrue(details["delivery_acceptance_received"])
        self.assertNotIn("api_receipt_received", details)
        relationships = list(owned_relationships(failure))

        self.assertIn(("local delivery failure", cause), relationships)
        self.assertIn(("handoff interruption", interruption), relationships)
