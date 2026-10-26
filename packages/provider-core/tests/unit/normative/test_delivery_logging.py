"""Docker-backed stream logging is independent of terminal result delivery."""

import errno
import io
import json
import unittest

from provider_core.api import ApiFailure
from provider_core.delivery_logging import DeliveryNotices
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.transport import HttpResponse


class DeliveryLoggingTests(unittest.TestCase):
    def test_record_larger_than_pipe_atomic_limit_still_reaches_the_owned_stream(self):
        output = io.StringIO()
        notices = DeliveryNotices(output, secrets=())
        self.addCleanup(notices.close)
        failures = []
        for _ in range(4):
            api = ApiFailure("publish Attempt failure", "owned diagnostic " * 500,
                             response=HttpResponse(503, ((b"x-request-id", b"request:delivery"),), b"PRIVATE_BODY"))
            failures.append(DeliveryWorkerFailed(cause=api, api_failure=api,
                cleanup_failures=(OSError(errno.EIO, "PRIVATE_NATIVE"),), api_receipt_received=False))
        notices(ExceptionGroup("Independent binding failures", failures), {"event": "delivery_failed"})
        self.assertGreater(len(output.getvalue().encode()), 4096)
        report = json.loads(output.getvalue())
        self.assertEqual(report["event"], "delivery_failed")
        self.assertIn("owned diagnostic", output.getvalue())
        self.assertIn("[truncated]", output.getvalue())
        self.assertIn('"status":503', output.getvalue())
        self.assertIn('"errno":5', output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(notices.status()["output_failures"], 0)

    def test_failed_output_retains_safe_health_and_never_raises_into_publication(self):
        class BrokenStream:
            def write(self, content):
                raise BrokenPipeError(errno.EPIPE, "PRIVATE_SINK_DETAIL")

            def flush(self):
                pass

        notices = DeliveryNotices(BrokenStream(), secrets=("PRIVATE_SECRET",))
        self.addCleanup(notices.close)
        notices(None, {"event": "delivery_published", "provider_ref": "provider:example"})
        status = notices.status()
        self.assertEqual(status["output_failures"], 1)
        self.assertEqual(status["last_failure_type"], "BrokenPipeError")
        self.assertEqual(status["last_failure_errno"], errno.EPIPE)
        self.assertEqual(status["last_failed_event"], "delivery_published")
        self.assertNotIn("PRIVATE", json.dumps(status))

    def test_known_credentials_and_credential_fields_are_redacted_before_stream_output(self):
        output = io.StringIO()
        notices = DeliveryNotices(output, secrets=("PRIVATE_SECRET",))
        self.addCleanup(notices.close)
        notices(ApiFailure("publish Attempt failure", 'token="PRIVATE_TOKEN"'), {
            "event": "delivery_retry", "safe_detail": "PRIVATE_SECRET",
        })
        self.assertNotIn("PRIVATE", output.getvalue())
