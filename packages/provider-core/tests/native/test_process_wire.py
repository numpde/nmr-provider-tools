"""Verify the private scientific process channel transports only portable records."""

from __future__ import annotations

import json
import socket
import traceback
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
    MAX_FAILURE_DEPTH,
    MAX_FAILURE_NODES,
    MAX_FRAME_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_TRACEBACK_FRAMES,
    MAX_TRACEBACK_LINE,
    MAX_TRACEBACK_LOCATION_CHARACTERS,
    RemoteScienceError,
    decode_frame,
    decoded_error_evidence,
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

    def test_child_traceback_locations_survive_causes_and_groups_without_local_payloads(self):
        def child_spectrum_calculation():
            _private_payload = {"sample": "child-local-payload-canary"}
            _live_model = object()
            raise ValueError("Distinctive child spectrum failure")

        try:
            child_spectrum_calculation()
        except ValueError as child_failure:
            location = child_failure.__traceback__

            while location.tb_next is not None:
                location = location.tb_next

            expected_location = {
                "file": location.tb_frame.f_code.co_filename,
                "function": location.tb_frame.f_code.co_name,
                "line": location.tb_lineno,
            }

            operation_failure = RuntimeError("Child operation failed")
            operation_failure.__cause__ = child_failure
            encoded = frame(ExceptionGroup("Child and cleanup failures", [operation_failure, OSError("Cleanup failed")]))

        received_group = decode_frame(encoded[4:])
        received = received_group.exceptions[0].__cause__
        evidence = decoded_error_evidence(received)
        wire_failure = json.loads(encoded[4:])["causes"][0]["cause"]
        rendered = "".join(traceback.format_exception(received_group))

        self.assertIs(expr1=ValueError, expr2=type(received))
        self.assertEqual(first="Distinctive child spectrum failure", second=str(received))
        self.assertEqual(
            first=(expected_location["file"], expected_location["function"], expected_location["line"]),
            second=evidence.frames[-1],
        )

        self.assertFalse(expr=evidence.frames_omitted)
        self.assertIsNone(obj=received.__traceback__)

        for location in wire_failure["frames"]:
            self.assertEqual(first={"file", "function", "line"}, second=set(location))

        for diagnostic in (encoded[4:].decode("ascii"), rendered):
            self.assertIn(member="child_spectrum_calculation", container=diagnostic)
            self.assertNotIn(member="child-local-payload-canary", container=diagnostic)
            self.assertNotIn(member="_live_model", container=diagnostic)

        self.assertIn(member=expected_location["file"], container=rendered)
        self.assertIn(member=str(expected_location["line"]), container=rendered)

    def test_child_traceback_limits_keep_innermost_frames_and_mark_omissions(self):
        for filename_length in (MAX_TRACEBACK_LOCATION_CHARACTERS, MAX_TRACEBACK_LOCATION_CHARACTERS + 1):
            with self.subTest(filename_length=filename_length):
                def child_recursive_calculation(depth):
                    if depth:
                        child_recursive_calculation(depth - 1)
                    else:
                        raise RuntimeError("Deep child failure")

                filename = "s" * filename_length
                child_recursive_calculation.__code__ = child_recursive_calculation.__code__.replace(co_filename=filename)

                try:
                    child_recursive_calculation(MAX_TRACEBACK_FRAMES + 4)
                except RuntimeError as child_failure:
                    encoded = frame(child_failure)

                received = decode_frame(encoded[4:])
                evidence = decoded_error_evidence(received)
                note = "\n".join(received.__notes__)

                self.assertTrue(expr=evidence.frames_omitted)
                self.assertIn(member="omitted", container=note)
                self.assertLessEqual(a=len(evidence.frames), b=MAX_TRACEBACK_FRAMES)
                self.assertLessEqual(a=len(note), b=MAX_DIAGNOSTIC_CHARACTERS + len(" [truncated]"))
                self.assertLess(a=len(encoded), b=MAX_DIAGNOSTIC_CHARACTERS * MAX_TRACEBACK_FRAMES)

                for (file, function, _) in evidence.frames:
                    self.assertLessEqual(a=len(file), b=MAX_TRACEBACK_LOCATION_CHARACTERS)
                    self.assertLessEqual(a=len(function), b=MAX_TRACEBACK_LOCATION_CHARACTERS)

                if filename_length == MAX_TRACEBACK_LOCATION_CHARACTERS:
                    self.assertEqual(first=MAX_TRACEBACK_FRAMES, second=len(evidence.frames))
                    self.assertEqual(first=filename, second=evidence.frames[-1][0])
                    self.assertEqual(first="child_recursive_calculation", second=evidence.frames[-1][1])
                    self.assertIn(member="truncated", container=note)
                else:
                    self.assertNotIn(member=filename, container=encoded[4:].decode("ascii"))

    def test_remote_traceback_notes_render_location_controls_as_literal_text(self):
        wire_failure = json.loads(frame(RuntimeError("Child failure"))[4:])
        wire_failure["frames"] = [{"file": "child\ncalculation.py", "function": "worker\x1b[31m", "line": 12}]
        received = decode_frame(json.dumps(wire_failure).encode("ascii"))
        note = "\n".join(received.__notes__)

        self.assertEqual(first=("child\ncalculation.py", "worker\x1b[31m", 12), second=decoded_error_evidence(received).frames[0])
        self.assertNotIn(member="child\ncalculation.py", container=note)
        self.assertNotIn(member="\x1b", container=note)
        self.assertIn(member=r"child\ncalculation.py", container=note)
        self.assertIn(member=r"worker\x1b[31m", container=note)

    def test_malformed_remote_traceback_evidence_is_refused(self):
        wire_failure = json.loads(frame(RuntimeError("Child failure"))[4:])
        valid_location = {"file": "child.py", "function": "child_calculation", "line": 12}
        invalid_frames = [None, {}, "frames", [None], [valid_location] * (MAX_TRACEBACK_FRAMES + 1)]

        for field in valid_location:
            invalid_frames.append([{key: value for (key, value) in valid_location.items() if key != field}])

        for extra_field in ("locals", "source"):
            invalid_frames.append([{**valid_location, extra_field: "private child evidence"}])

        for field in ("file", "function"):
            for value in (None, False, 12, [], {}, "", "x" * (MAX_TRACEBACK_LOCATION_CHARACTERS + 1)):
                invalid_frames.append([{**valid_location, field: value}])

        for value in (None, False, True, 0, -1, 1.5, "12", MAX_TRACEBACK_LINE + 1):
            invalid_frames.append([{**valid_location, "line": value}])

        invalid_evidence = [{**wire_failure, "frames": frames} for frames in invalid_frames]
        invalid_evidence.extend({**wire_failure, "frames_omitted": value} for value in (None, 0, 1, "false", [], {}))

        for missing_field in ("frames", "frames_omitted"):
            invalid_evidence.append({key: value for (key, value) in wire_failure.items() if key != missing_field})

        for error_fields in invalid_evidence:
            with self.subTest(error_fields=error_fields), self.assertRaises(expected_exception=ValueError):
                decode_frame(json.dumps(error_fields).encode("ascii"))

    def test_decoded_failure_facts_compare_across_replies_and_distinguish_changed_causes(self):
        rejection = ScientificRejection(code="missing_spectrum", message="Supply a spectrum")
        rejection.__cause__ = ValueError("Spectrum calibration is missing")
        encoded = frame(ExceptionGroup("Scientific failures", [rejection]))[4:]
        first = decode_frame(encoded)
        repeated = decode_frame(encoded)
        evidence = decoded_error_evidence(first)

        self.assertIsNot(expr1=first.exceptions[0], expr2=repeated.exceptions[0])
        self.assertEqual(first=evidence, second=decoded_error_evidence(repeated))
        self.assertIsNone(obj=decoded_error_evidence(ValueError("Local failure")))

        for change in ("code", "cause_message", "causal_relationship", "frame"):
            with self.subTest(change=change):
                changed = json.loads(encoded)
                child = changed["causes"][0]

                if change == "code":
                    child["code"] = "invalid_spectrum"
                elif change == "cause_message":
                    child["cause"]["message"] = "Spectrum calibration is inconsistent"
                elif change == "causal_relationship":
                    child["context"] = child.pop("cause")
                else:
                    child["cause"]["frames"] = [{"file": "child.py", "function": "calibrate_spectrum", "line": 12}]

                received = decode_frame(json.dumps(changed).encode("ascii"))

                self.assertNotEqual(first=evidence, second=decoded_error_evidence(received))

    def test_decoder_graph_bounds_admit_encoder_limit_markers(self):
        failure = RuntimeError("Deep scientific failure")

        for _ in range(MAX_FAILURE_DEPTH):
            failure = ExceptionGroup(
                "Independent failure " * MAX_DIAGNOSTIC_CHARACTERS,
                [failure] + [OSError("Cleanup failed") for _ in range(MAX_FAILURE_NODES)],
            )
            failure.__cause__ = RuntimeError("Group cause " * MAX_DIAGNOSTIC_CHARACTERS)

        received = roundtrip(failure)
        pending = [decoded_error_evidence(received)]
        (count, omissions) = (0, 0)

        while pending:
            evidence = pending.pop()
            count += 1
            omissions += evidence.frames_omitted
            pending.extend(evidence.causes)

            for cause in (evidence.cause, evidence.context):
                if cause is not None:
                    pending.append(cause)

        self.assertIn(member="truncated", container=received.message)
        self.assertGreater(a=count, b=MAX_FAILURE_NODES)
        self.assertLessEqual(a=count, b=2 * MAX_FAILURE_NODES)
        self.assertGreater(a=omissions, b=0)

    def test_malformed_failure_graphs_and_oversized_descriptions_are_refused(self):
        leaf = json.loads(frame(RuntimeError("Child failure"))[4:])
        group = json.loads(frame(ExceptionGroup("Scientific failures", [RuntimeError("Child failure")]))[4:])
        too_deep = leaf

        for _ in range(MAX_FAILURE_DEPTH + 1):
            too_deep = {**leaf, "cause": too_deep}

        invalid_evidence = [
            {**leaf, "message": "m" * (MAX_DIAGNOSTIC_CHARACTERS + len(" [truncated]") + 1)},
            {**leaf, "original_type": "t" * (MAX_DIAGNOSTIC_CHARACTERS + len(" [truncated]") + 1)},
            {**leaf, "cause": {"mapping": {}}},
            {**leaf, "context": None},
            {**group, "causes": [leaf] * (MAX_FAILURE_NODES + 1)},
            {**group, "causes": [{**group, "causes": [leaf] * MAX_FAILURE_NODES}] * 3},
            too_deep,
        ]

        for error_fields in invalid_evidence:
            with self.subTest(error_fields=error_fields), self.assertRaises(expected_exception=ValueError):
                decode_frame(json.dumps(error_fields).encode("ascii"))

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
