"""Docker-backed stream logging is independent of terminal result delivery."""

import errno
import io
import json
import unittest
import os

from provider_core.api_errors import ApiFailure
from provider_core.delivery_logging import DeliveryNotices
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.transport import HttpResponse


class DeliveryLoggingTests(unittest.TestCase):
    def _large_log_record(self):
        output = io.StringIO()
        notices = DeliveryNotices(output, secrets=())

        self.addCleanup(notices.close)
        failures = []

        for _ in range(4):
            api = ApiFailure(
                "publish Attempt failure",
                "owned diagnostic " * 500,
                response=HttpResponse(503, ((b"x-request-id", b"request:delivery"),), b"PRIVATE_BODY")
            )

            failures.append(
                DeliveryWorkerFailed(
                    cause=api,
                    api_failure=api,
                    cleanup_failures=(OSError(errno.EIO, "PRIVATE_NATIVE"),),
                    api_receipt_received=False
                )
            )

        notices(ExceptionGroup("Independent binding failures", failures), {"event": "delivery_failed"})

        return (output.getvalue(), notices)

    def test_record_larger_than_pipe_atomic_limit_still_reaches_the_owned_stream(self):
        (rendered, notices) = self._large_log_record()

        self.assertGreater(len(rendered.encode()), 4096)
        report = json.loads(rendered)

        self.assertEqual(report["event"], "delivery_failed")
        self.assertIn("owned diagnostic", rendered)
        self.assertIn("[truncated]", rendered)
        self.assertIn('"status":503', rendered)
        self.assertIn('"errno":5', rendered)
        self.assertEqual(notices.status()["output_failures"], 0)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_large_log_record_omits_response_and_native_text(self):
        (rendered, _notices) = self._large_log_record()

        self.assertNotIn("PRIVATE", rendered)

    def _failed_output_status(self):
        class BrokenStream:
            def write(self, content):
                raise BrokenPipeError(errno.EPIPE, "PRIVATE_SINK_DETAIL")

            def flush(self):
                pass

        notices = DeliveryNotices(BrokenStream(), secrets=("PRIVATE_SECRET",))

        self.addCleanup(notices.close)
        notices(None, {"event": "delivery_published", "provider_ref": "provider:example"})

        return notices.status()

    def test_failed_output_retains_safe_health_and_never_raises_into_publication(self):
        status = self._failed_output_status()

        self.assertEqual(status["output_failures"], 1)
        self.assertEqual(status["last_failure_type"], "BrokenPipeError")
        self.assertEqual(status["last_failure_errno"], errno.EPIPE)
        self.assertEqual(status["last_failed_event"], "delivery_published")

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_output_health_omits_sink_details(self):
        self.assertNotIn("PRIVATE", json.dumps(self._failed_output_status()))

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_known_credentials_and_credential_fields_are_redacted_before_stream_output(self):
        output = io.StringIO()
        notices = DeliveryNotices(output, secrets=("PRIVATE_SECRET",))

        self.addCleanup(notices.close)

        notices(
            ApiFailure("publish Attempt failure", 'token="PRIVATE_TOKEN"'),
            {
                "event": "delivery_retry",
                "safe_detail": "PRIVATE_SECRET",
            }
        )

        self.assertNotIn("PRIVATE", output.getvalue())
