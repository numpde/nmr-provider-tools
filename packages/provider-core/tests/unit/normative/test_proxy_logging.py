"""Proxy logs expose original failures while the formatter scrubs credentials."""

import io
import logging
import unittest
from unittest.mock import MagicMock, patch

from provider_core import bound_http_proxy
from provider_core.diagnostic_text import CredentialRedactingFormatter
from tests.fakes.proxy_logs import capture_proxy_logs


class ProxyLoggingTests(unittest.TestCase):
    def test_formatter_scrubs_output_without_changing_record_or_exception(self):
        cause = OSError(32, "Broken pipe; Bearer credential-in-cause")
        failure = RuntimeError("Forwarding stopped; api_key=credential-in-message")
        failure.__cause__ = cause
        failure.add_note("upstream-to-client: write 24 bytes; token=credential-in-note")
        record = logging.LogRecord(
            "provider_core.proxy", logging.ERROR, __file__, 1,
            "Operation failed; password=%s", ("credential-in-argument",),
            (type(failure), failure, failure.__traceback__),
        )
        formatted = CredentialRedactingFormatter("%(message)s").format(record)
        for secret in ("credential-in-cause", "credential-in-message", "credential-in-note", "credential-in-argument"):
            self.assertNotIn(secret, formatted)
        for fact in ("Broken pipe", "Errno 32", "RuntimeError", "upstream-to-client", "write 24 bytes"):
            self.assertIn(fact, formatted)
        self.assertIs(record.exc_info[1], failure)
        self.assertIs(failure.__cause__, cause)
        self.assertIn("credential-in-message", str(failure))
        self.assertIn("credential-in-note", failure.__notes__[0])
        self.assertEqual(record.args, ("credential-in-argument",))

    def test_relay_logs_direction_and_operation_on_the_original_native_failure(self):
        """Never infer HTTP success or zero delivery from a failed socket write."""
        for upstream in (False, True):
            for operation in ("read", "write", "half-close"):
                with self.subTest(upstream=upstream, operation=operation):
                    left, right = MagicMock(), MagicMock()
                    source, destination = (right, left) if upstream else (left, right)
                    direction = "upstream-to-client" if upstream else "client-to-upstream"
                    original = BrokenPipeError(32, "Broken pipe")
                    source.recv.return_value = b"x" * 24
                    if operation == "read":
                        source.recv.side_effect = original
                    elif operation == "write":
                        destination.sendall.side_effect = original
                    else:
                        source.recv.return_value = b""
                        destination.shutdown.side_effect = original
                    output = io.StringIO()
                    with patch.object(bound_http_proxy.select, "select", return_value=([source], [], [])):
                        try:
                            bound_http_proxy.relay(left, right)
                        except BrokenPipeError as observed:
                            self.assertIs(observed, original)
                            with capture_proxy_logs(output):
                                bound_http_proxy.log_failure("Proxy forwarding", observed)
                        else:
                            self.fail("The native socket failure must propagate")
                    for fact in (direction, operation, "BrokenPipeError", "Broken pipe", "relay"):
                        self.assertIn(fact, output.getvalue())
                    if operation == "write":
                        self.assertIn("24 bytes", output.getvalue())
                        self.assertIn("may be partial", output.getvalue())

    def test_worker_failure_keeps_traceback_and_original_reason(self):
        output = io.StringIO()
        failure = RuntimeError("worker could not start")
        with capture_proxy_logs(output):
            try:
                raise failure
            except RuntimeError:
                bound_http_proxy.ProxyServer.handle_error(None, None, None)
        self.assertIn("worker could not start", output.getvalue())
        self.assertIn("test_worker_failure", output.getvalue())
