"""Verify the private scientific process channel transports only portable records."""

from __future__ import annotations

import unittest

from provider_core.contribution import CallRef
from provider_core.contribution_errors import CallRejected, ScientificRejection
from provider_core.hosting import (
    AcceptanceState, DispatchState, Failure, Invocation, Observation,
    OperationPolicy, OperationRef, Outcome, ReservationRef, ResourceState,
    ResultState,
)
from provider_core.process_wire import RemoteScienceError, decode_frame, frame


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

    def test_foreign_failure_is_not_turned_into_input_rejection(self):
        reconstructed = roundtrip(LookupError("Lost scientific worker"))

        self.assertIsInstance(reconstructed, RemoteScienceError)
        self.assertNotIsInstance(reconstructed, CallRejected)

    def test_oversized_channel_message_is_refused(self):
        from provider_core.process_wire import MAX_FRAME_BYTES

        with self.assertRaises(ValueError):
            frame(b"x" * MAX_FRAME_BYTES)
