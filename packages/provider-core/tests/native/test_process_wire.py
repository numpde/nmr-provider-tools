"""Verify the private scientific process channel transports only portable records."""

from __future__ import annotations

import unittest
from unittest.mock import patch
import socket

from provider_core.contribution import CallRef
from provider_core.contribution_errors import CallRejected, ScientificRejection
from provider_core.hosting import (
    AcceptanceState, DispatchState, Failure, Invocation, Observation,
    OperationPolicy, OperationRef, Outcome, ReservationRef, ResourceState,
    ResultState, StartDeadlineExpired,
)
from provider_core.process_wire import (
    MAX_DIAGNOSTIC_CHARACTERS, MAX_PAYLOAD_BYTES,
    RemoteScienceError, decode_frame, frame, receive,
)


def roundtrip(value):
    return decode_frame(frame(value)[4:])


class ProcessWireTests(unittest.TestCase):
    def test_invocation_preserves_bytes_identity_and_policy(self):
        reservation = ReservationRef("host", "analysis", "allocation", "binding", "sha256:" + "a" * 64)
        policy = OperationPolicy(5, 5, 5, 1000, 1000)
        invocation = Invocation(
            operation_id="operation",
            target=CallRef(reservation.binding_id, reservation.binding_revision, "prepare"),
            payload=b"\x00\xff\x01",
            states={},
            reservation=reservation,
            policy=policy,
        )

        self.assertEqual(invocation, roundtrip(invocation))

    def test_scientific_rejection_retains_the_correctable_outcome(self):
        operation = OperationRef("host", "analysis", "operation")
        observation = Observation(
            operation=operation,
            target=CallRef("binding", "sha256:" + "a" * 64, "prepare"),
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

        self.assertIs(Outcome.REJECTED, received.outcome)
        self.assertEqual("calculation", received.failures[0].phase)
        self.assertIsInstance(received.failures[0].cause, CallRejected)
        self.assertEqual("missing_science", received.failures[0].cause.code)

    def test_custom_scientific_rejection_keeps_correction_permission(self):
        class ProviderRejection(CallRejected):
            pass

        reconstructed = roundtrip(ProviderRejection(code="invalid_spectrum", message="Bad spectrum"))

        self.assertIs(type(reconstructed), CallRejected)
        self.assertEqual("invalid_spectrum", reconstructed.code)
        self.assertEqual("Bad spectrum", reconstructed.message)

    def test_foreign_failure_is_not_turned_into_input_rejection(self):
        reconstructed = roundtrip(LookupError("Lost scientific worker"))

        self.assertIsInstance(reconstructed, RemoteScienceError)
        self.assertNotIsInstance(reconstructed, CallRejected)

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
                self.assertIs(type(received), CallRejected)
                self.assertEqual("bad_spectrum", received.code)
                self.assertEqual("Supply proton peaks", received.message)

        failure = BrokenForeignError()
        failure.__cause__ = OSError("Checkpoint unavailable")
        received = roundtrip(failure)
        self.assertIs(type(received), RemoteScienceError)
        self.assertEqual(f"{BrokenForeignError.__module__}.{BrokenForeignError.__qualname__}", received.remote_type)
        self.assertIn("Failure text unavailable", str(received))
        self.assertIs(type(received.__cause__), OSError)
        self.assertEqual("Checkpoint unavailable", str(received.__cause__))

    def test_foreign_exception_names_do_not_grant_sdk_semantics(self):
        for name in ("CallRejected", "ScientificRejection", "StartDeadlineExpired"):
            with self.subTest(name=name):
                foreign_type = type(name, (Exception,), {"__module__": "provider_fixture"})
                received = roundtrip(foreign_type("Foreign failure"))

                self.assertIs(type(received), RemoteScienceError)
                self.assertEqual("provider_fixture." + name, received.remote_type)

        self.assertIs(type(roundtrip(StartDeadlineExpired("Too late"))), StartDeadlineExpired)

    def test_independent_failures_retain_explicit_and_implicit_causes(self):
        loading = RuntimeError("Model loading failed")
        loading.__cause__ = OSError("Checkpoint could not be read")
        cleanup = RuntimeError("Model cleanup failed")
        cleanup.__context__ = LookupError("Device owner vanished")
        received = roundtrip(ExceptionGroup("Startup and cleanup", [loading, cleanup]))

        self.assertIsInstance(received, ExceptionGroup)
        self.assertEqual("Startup and cleanup", received.message)
        self.assertEqual(2, len(received.exceptions))
        first, second = received.exceptions
        self.assertIs(type(first.__cause__), OSError)
        self.assertEqual("Checkpoint could not be read", str(first.__cause__))
        self.assertIs(type(second.__context__), RemoteScienceError)
        self.assertIn("Device owner vanished", str(second.__context__))

    def test_suppressed_context_stays_suppressed(self):
        failure = RuntimeError("Reported failure")
        failure.__context__ = OSError("Hidden context")
        failure.__suppress_context__ = True

        self.assertIsNone(roundtrip(failure).__context__)

    def test_cyclic_and_large_failures_leave_bounded_explicit_diagnostics(self):
        failure = RuntimeError("failure " * MAX_DIAGNOSTIC_CHARACTERS)
        failure.__cause__ = failure
        received = roundtrip(failure)

        self.assertIn("truncated", str(received))
        self.assertIs(type(received.__cause__), RemoteScienceError)
        self.assertIn("cycle or diagnostic limit", str(received.__cause__))

        failures = ExceptionGroup("Many failures", [OSError("Cleanup failed") for _ in range(100)])
        received_group = roundtrip(failures)
        self.assertLess(len(received_group.exceptions), len(failures.exceptions))
        self.assertIn("omitted", str(received_group.exceptions[-1]))

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
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_frame(payload)

    def test_supported_payload_fits_a_reply_and_excess_is_refused(self):
        payload = b"x" * MAX_PAYLOAD_BYTES
        self.assertEqual(payload, roundtrip({"ok": True, "value": payload})["value"])

        with self.assertRaises(ValueError):
            frame(payload + b"x")

    def test_header_and_body_share_one_receive_deadline(self):
        parent, child = socket.socketpair()

        try:
            child.sendall(frame({"ok": True, "value": None}))

            with patch("provider_core.process_wire.time.monotonic", side_effect=(10, 12)):
                with self.assertRaises(TimeoutError):
                    receive(parent, deadline=11)
        finally:
            parent.close()
            child.close()

    def test_oversized_channel_message_is_refused(self):
        from provider_core.process_wire import MAX_FRAME_BYTES

        with self.assertRaises(ValueError):
            frame("x" * MAX_FRAME_BYTES)
