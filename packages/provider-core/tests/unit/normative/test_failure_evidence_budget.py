"""Protected roots budget diagnostic prose without changing scientific outcomes."""

import json
import unittest

from provider_core.attempt_record import MAX_EVIDENCE_BYTES, ProtectedEvidence
from provider_core.diagnostics import DiagnosticOutputRefused, failure_evidence, failure_evidence_json
from provider_core.payload import PayloadRejected
from provider_core.process_wire import MAX_DIAGNOSTIC_CHARACTERS, MAX_FAILURE_NODES, MAX_TRACEBACK_FRAMES, MAX_TRACEBACK_LOCATION_CHARACTERS, decode_frame, frame


def maximal_remote_failure():
    """Exercise legal Unicode wire evidence through the actual packer and decoder."""

    name = "\U00010400" * MAX_TRACEBACK_LOCATION_CHARACTERS
    namespace = {}

    source = (
        f"def {name}(depth, message):\n"
        f"    if depth:\n"
        f"        return {name}(depth - 1, message)\n"
        f"    raise RuntimeError(message)\n"
    )

    exec(compile(source, name, "exec"), namespace)
    failures = []

    for _ in range(MAX_FAILURE_NODES):
        try:
            namespace[name](MAX_TRACEBACK_FRAMES, "\U00010400" * MAX_DIAGNOSTIC_CHARACTERS)
        except RuntimeError as failure:
            failures.append(failure)

    encoded = frame(ExceptionGroup("Scientific cleanup failures", failures))

    return decode_frame(encoded[4:])


class FailureEvidenceBudgetTests(unittest.TestCase):
    def test_legal_remote_graph_and_multiple_failures_fit_one_protected_root(self):
        received = maximal_remote_failure()
        ordinary = failure_evidence(received, secrets=())

        # Reproduce the consumer overflow before exercising aggregate projection
        self.assertGreater(len(json.dumps(ordinary, ensure_ascii=False).encode("utf-8")), MAX_EVIDENCE_BYTES)

        facts = {"science": {"outcome": "succeeded", "failures": [received, received]}}
        content = failure_evidence_json(facts, secrets=(), maximum_bytes=MAX_EVIDENCE_BYTES)
        protected = ProtectedEvidence("test.science.v1", content)
        observed = json.loads(protected.content)

        self.assertLessEqual(len(content), MAX_EVIDENCE_BYTES)
        self.assertEqual(observed["science"]["outcome"], "succeeded")
        self.assertEqual(len(observed["science"]["failures"]), 2)
        self.assertIn("[truncated]", content.decode("utf-8"))

        def structure(fact):
            if type(fact) is list:
                return [structure(member) for member in fact]

            if type(fact) is dict:
                return {
                    name: structure(member)
                    for (name, member) in fact.items()
                    if name not in {"message", "file", "function"}
                }

            return fact

        for projected in observed["science"]["failures"]:
            self.assertEqual(structure(projected), structure(ordinary))

        self.assertEqual(facts["science"]["failures"], [received, received])
        self.assertEqual(failure_evidence(received, secrets=()), ordinary)

    def test_small_root_keeps_existing_projection_and_owner_facts(self):
        failure = OSError(2, "unreviewed filename")
        expected = {"science": {"outcome": "failed", "failure": failure_evidence(failure, secrets=())}}

        content = failure_evidence_json(
            {"science": {"outcome": "failed", "failure": failure}},
            secrets=(),
            maximum_bytes=MAX_EVIDENCE_BYTES,
        )

        self.assertEqual(json.loads(content), expected)

    def test_wire_surrogates_and_rejection_code_remain_representable(self):
        failure = PayloadRejected("\ud800" * MAX_DIAGNOSTIC_CHARACTERS, code=PayloadRejected.Code.INVALID_VALUE)
        received = decode_frame(frame(failure)[4:])
        content = failure_evidence_json({"failure": received}, secrets=(), maximum_bytes=700)
        projected = json.loads(content)["failure"]

        self.assertLessEqual(len(content), 700)
        self.assertEqual(projected["remote_code"], PayloadRejected.Code.INVALID_VALUE.value)
        self.assertEqual(projected["remote_kind"], "CallRejected")
        self.assertIn("\ud800", projected["message"])
        self.assertIn("[truncated]", projected["message"])

    def test_redacts_complete_text_before_budgeting_and_observes_owners_once(self):
        secret = "private-fragment-" * 80
        failure = RuntimeError()
        failure.__cause__ = failure
        calls = []

        def describe_owner(error, text, diagnostic_window):
            calls.append(error)

            return {
                "message": text(secret + ('"\\\n' * 300) + ("\U00010400" * 300), 2048),
                "required_status": "succeeded",
            }

        content = failure_evidence_json(
            {"failure": failure},
            secrets=iter([secret]),
            maximum_bytes=700,
            describe_owner=describe_owner,
        )

        projected = json.loads(content)["failure"]

        self.assertLessEqual(len(content), 700)
        self.assertNotIn(secret, content.decode("utf-8"))
        self.assertNotIn("private-fragment", content.decode("utf-8"))
        self.assertIn("[redacted]", projected["message"])
        self.assertIn("[truncated]", projected["message"])
        self.assertEqual(calls, [failure])
        self.assertEqual(projected["required_status"], "succeeded")
        self.assertEqual(projected["relationships"], [{"relationship": "cause", "failure": {"exception_reference": 0}}])
        self.assertIs(failure.__cause__, failure)

    def test_required_root_overflow_is_refused_without_replacing_outcome(self):
        facts = {"outcome": "succeeded", "required_record": "x" * 1000, "failure": RuntimeError()}

        with self.assertRaises(DiagnosticOutputRefused):
            failure_evidence_json(facts, secrets=(), maximum_bytes=100)

        self.assertEqual(facts["outcome"], "succeeded")


if __name__ == "__main__":
    unittest.main()
