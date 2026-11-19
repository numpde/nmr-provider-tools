"""Only complete, correlated problems acquire their reviewed API meaning."""

import json
import unittest

from provider_core.transport import HttpResponse
from provider_core.validation.upstream.nmr.v1.response_envelope import read_problem


class ResponseProblemTests(unittest.TestCase):
    def _problem_replies(self):
        document = {
            "type": "urn:nmr-api:problem:authentication-failed",
            "title": "private-title-canary",
            "status": 401,
            "instance": "urn:nmr-api:request:test",
            "request_id": "request:test",
            "code": "signature_expired",
            "detail": "private-detail-canary",
        }

        headers = ((b"content-type", b"application/problem+json"), (b"x-request-id", b"request:test"))

        for (changes, known) in (
            ({}, True),
            ({"status": 404}, False),
            ({"code": "private-code-canary"}, False),
            ({"type": "urn:foreign"}, False),
            (
                {
                    "request_id": (
                        "bad\n"
                        "request"
                    )
                },
                False
            ),
        ):
            body = json.dumps(document | changes).encode()

            yield (HttpResponse(401, headers, body), known)

        duplicate = json.dumps(document)[:-1] + ',"code":"signature_expired"}'

        yield (HttpResponse(401, headers, duplicate.encode()), False)

    def test_problem_reason_is_local_and_rejects_unknown_or_ambiguous_problems(self):
        for (response, known) in self._problem_replies():
            with self.subTest(body=response.body):
                problem = read_problem(response)

                if known:
                    (problem_code, reason) = problem

                    self.assertEqual(problem_code, "signature_expired")
                    self.assertIn("expired", reason)
                    self.assertIn("clock", reason)
                else:
                    self.assertIsNone(problem)
