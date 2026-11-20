"""Verify the private scientific process channel transports only portable records."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from provider_core.contribution import CallRef
from provider_core.contribution_errors import CallRejected, ScientificRejection
from provider_core.hosting import (
    AcceptanceState,
    DispatchState,
    Failure,
    Invocation,
    Observation,
    OperationPolicy,
    OperationRef,
    Outcome,
    ReservationRef,
    ResourceState,
    ResultState,
    StartDeadlineExpired,
)
from provider_core.process_wire import (
    MAX_DIAGNOSTIC_CHARACTERS,
    MAX_FRAME_BYTES,
    MAX_PAYLOAD_BYTES,
    RemoteScienceError,
    decode_frame,
    frame,
    receive,
)


def roundtrip(sdk_value):
    return decode_frame(frame(sdk_value)[4:])


class ProcessWireTests(unittest.TestCase):
    def test_invocation_preserves_bytes_identity_and_policy(self):
        reservation = ReservationRef("host", "analysis", "allocation", "binding", "sha256:" + ("a" * 64))
        policy = OperationPolicy(5, 5, 5, 1000, 1000)

        invocation = Invocation(
            operation_id="operation",
            target=CallRef(reservation.binding_id, reservation.binding_revision, "prepare"),
            payload=b"\x00\xff\x01",
            states={},
            reservation=reservation,
            policy=policy,
        )

        self.assertEqual(first=invocation, second=roundtrip(invocation))

    def test_scientific_rejection_retains_the_correctable_outcome(self):
        operation = OperationRef("host", "analysis", "operation")
        observation = Observation(
            operation=operation,
            target=CallRef("binding", "sha256:" + ("a" * 64), "prepare"),
            revision=1,
            acceptance=AcceptanceState.OWNED,
            dispatch=DispatchState.STARTED,
            finished=True,
            outcome=Outcome.REJECTED,
            result_state=ResultState.NONE,
            result=None,
            states=(),
            progress=(),
            progress_dropped=0,
            failures=(Failure("calculation", ScientificRejection(code="missing_science", message="Supply formula")),),
            stop_requested=False,
            allowance_expired=False,
            resources=ResourceState.RELEASED,
        )

        received = roundtrip(observation)

        self.assertIs(expr1=Outcome.REJECTED, expr2=received.outcome)
        self.assertEqual(first="calculation", second=received.failures[0].phase)
        self.assertIsInstance(obj=received.failures[0].cause, cls=CallRejected)
        self.assertEqual(first="missing_science", second=received.failures[0].cause.code)

    def test_custom_scientific_rejection_keeps_correction_permission(self):
        class ProviderRejection(CallRejected):
            pass

        reconstructed = roundtrip(ProviderRejection(code="invalid_spectrum", message="Bad spectrum"))

        self.assertIs(expr1=CallRejected, expr2=type(reconstructed))
        self.assertEqual(first="invalid_spectrum", second=reconstructed.code)
        self.assertEqual(first="Bad spectrum", second=reconstructed.message)

    def test_foreign_failure_is_not_turned_into_input_rejection(self):
        reconstructed = roundtrip(LookupError("Lost scientific worker"))

        self.assertIsInstance(obj=reconstructed, cls=RemoteScienceError)
        self.assertNotIsInstance(obj=reconstructed, cls=CallRejected)

    def test_custom_error_rendering_cannot_erase_failure_evidence(self):
        class EmptyRejection(CallRejected):
            def __str__(self):
                return ""

        class BrokenRejection(CallRejected):
            def __str__(self):
                raise ValueError("Cannot render rejection")

        class BrokenForeignError(Exception):
            def __str__(self):
                raise ValueError("Cannot render failure")

        for rejection_type in (EmptyRejection, BrokenRejection):
            with self.subTest(rejection_type=rejection_type):
                received = roundtrip(rejection_type(code="bad_spectrum", message="Supply proton peaks"))

                self.assertIs(expr1=CallRejected, expr2=type(received))
                self.assertEqual(first="bad_spectrum", second=received.code)
                self.assertEqual(first="Supply proton peaks", second=received.message)

        failure = BrokenForeignError()
        failure.__cause__ = OSError("Checkpoint unavailable")
        received = roundtrip(failure)

        self.assertIs(expr1=RemoteScienceError, expr2=type(received))

        expected_type_name = f"{BrokenForeignError.__module__}.{BrokenForeignError.__qualname__}"  # machine-string: The reconstructed remote_type must equal the wire's dotted Python type identity

        self.assertEqual(first=expected_type_name, second=received.remote_type)
        self.assertIn(member="Failure text unavailable", container=str(received))
        self.assertIs(expr1=OSError, expr2=type(received.__cause__))
        self.assertEqual(first="Checkpoint unavailable", second=str(received.__cause__))

    def test_foreign_exception_names_do_not_grant_sdk_semantics(self):
        for name in ("CallRejected", "ScientificRejection", "StartDeadlineExpired"):
            with self.subTest(name=name):
                foreign_type = type(name, (Exception,), {"__module__": "provider_fixture"})
                received = roundtrip(foreign_type("Foreign failure"))

                self.assertIs(expr1=RemoteScienceError, expr2=type(received))
                self.assertEqual(first="provider_fixture." + name, second=received.remote_type)

        self.assertIs(expr1=StartDeadlineExpired, expr2=type(roundtrip(StartDeadlineExpired("Too late"))))

    def test_independent_failures_retain_explicit_and_implicit_causes(self):
        loading = RuntimeError("Model loading failed")
        loading.__cause__ = OSError("Checkpoint could not be read")
        cleanup = RuntimeError("Model cleanup failed")
        cleanup.__context__ = LookupError("Device owner vanished")
        received = roundtrip(ExceptionGroup("Startup and cleanup", [loading, cleanup]))

        self.assertIsInstance(obj=received, cls=ExceptionGroup)
        self.assertEqual(first="Startup and cleanup", second=received.message)
        self.assertEqual(first=2, second=len(received.exceptions))

        (loading_failure, cleanup_failure) = received.exceptions

        self.assertIs(expr1=OSError, expr2=type(loading_failure.__cause__))
        self.assertEqual(first="Checkpoint could not be read", second=str(loading_failure.__cause__))
        self.assertIs(expr1=RemoteScienceError, expr2=type(cleanup_failure.__context__))
        self.assertIn(member="Device owner vanished", container=str(cleanup_failure.__context__))

    def test_suppressed_context_stays_suppressed(self):
        failure = RuntimeError("Reported failure")
        failure.__context__ = OSError("Hidden context")
        failure.__suppress_context__ = True

        self.assertIsNone(obj=roundtrip(failure).__context__)

    def test_cyclic_and_large_failures_leave_bounded_explicit_diagnostics(self):
        failure = RuntimeError("failure " * MAX_DIAGNOSTIC_CHARACTERS)
        failure.__cause__ = failure
        received = roundtrip(failure)

        self.assertIn(member="truncated", container=str(received))
        self.assertIs(expr1=RemoteScienceError, expr2=type(received.__cause__))
        self.assertIn(member="cycle or diagnostic limit", container=str(received.__cause__))

        failures = ExceptionGroup("Many failures", [OSError("Cleanup failed") for _ in range(100)])
        received_group = roundtrip(failures)

        self.assertLess(a=len(received_group.exceptions), b=len(failures.exceptions))
        self.assertIn(member="omitted", container=str(received_group.exceptions[-1]))

    def test_invalid_frames_fail_as_protocol_values(self):
        for payload in (
            b'{"record":"NotAnSdkRecord","fields":{}}',
            b'{"mapping":[]}',
            b'{"bytes":"","tuple":[]}',
            b'{"bytes":"","bytes":""}',
            b'{"enum":"Outcome","value":"not_an_outcome"}',
            b'{"error":"CallRejected","message":"Missing code"}',
            b'NaN',
            b'1e999',
        ):
            with self.subTest(payload=payload), self.assertRaises(expected_exception=ValueError):
                decode_frame(payload)

    def test_supported_payload_fits_a_reply_and_excess_is_refused(self):
        payload = b"x" * MAX_PAYLOAD_BYTES

        self.assertEqual(first=payload, second=roundtrip({"ok": True, "value": payload})["value"])

        with self.assertRaises(expected_exception=ValueError):
            frame(payload + b"x")

    def test_header_and_body_share_one_receive_deadline(self):
        (parent_socket, child_socket) = socket.socketpair()

        try:
            child_socket.sendall(frame({"ok": True, "value": None}))

            with patch("provider_core.process_wire.time.monotonic", side_effect=(10, 12)):
                with self.assertRaises(expected_exception=TimeoutError):
                    receive(parent_socket, deadline=11)
        finally:
            parent_socket.close()
            child_socket.close()

    def test_oversized_channel_message_is_refused(self):
        with self.assertRaises(expected_exception=ValueError):
            frame("x" * MAX_FRAME_BYTES)
