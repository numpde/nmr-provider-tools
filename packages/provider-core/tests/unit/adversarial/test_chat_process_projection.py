"""Child diagnostics preserve the parent's failure without expanding disclosure."""

import asyncio
import signal
import unittest
import os

from provider_core.chat_errors import ChatFailed
from provider_core.chat_process import ChatProcessResult, SupervisedChatTransport, _Exchange
from provider_core.diagnostics import failure_evidence
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.interpreter import _retryable
from provider_core.transport import HttpResponseHead
from provider_core.transport_errors import ExchangeFailed


class ChatProcessProjectionTests(unittest.TestCase):
    def test_admitted_child_caller_interruption_cannot_be_erased_into_retry_permission(self):
        child_interruption = asyncio.CancelledError("the original child caller stopped")
        parent_interruption = asyncio.CancelledError("the original parent caller stopped")

        for (parent, expected) in ((None, child_interruption), (parent_interruption, parent_interruption)):
            with self.subTest(parent_interrupted=parent is not None):
                child = ExchangeFailed(
                    "The child exchange was interrupted.",
                    primary=TimeoutError("response deadline"),
                    proxy_connect_status=503,
                    caller_interruption=child_interruption,
                )

                failure = ChatFailed("exchange_failed", "The model request failed.", cause=child)

                self.assertFalse(_retryable(failure))

                exchange = _Exchange(
                    object(),
                    1.0,
                    failure=child,
                    child_reply=child,
                    caller_interruption=parent,
                    retired=True,
                )

                # Exercise the actual parent evidence merge after admitting a
                # child reply; no process or TLS setup is needed for this state.
                transport = SupervisedChatTransport.__new__(SupervisedChatTransport)

                transport._update_failure(exchange)
                self.assertIs(child, transport.last_failure)
                self.assertIs(expected, child.caller_interruption)
                self.assertFalse(_retryable(failure))

    def _child_exchange_failure(self):
        class ProtectedChildError(Exception):
            def __str__(self):
                raise AssertionError("Unreviewed child exception text must remain protected")

        class UnreviewedProcess:
            @property
            def pid(self):
                raise AssertionError("An unreviewed process-shaped object must not be inspected")

        deadline = TimeoutError("original model deadline")

        child = ExchangeFailed(
            "The original child could not retire its HTTP exchange.",
            primary=ProtectedChildError("PRIVATE_CHILD_TEXT"),
            response_head=HttpResponseHead(503, ((b"set-cookie", b"PRIVATE_CHILD_HEADER"),)),
        )

        failure = ExchangeFailed("The model exchange exceeded its original deadline.", primary=deadline)
        failure.child_failure = child
        failure.process = ChatProcessResult(1234, -int(signal.SIGKILL), (int(signal.SIGTERM), int(signal.SIGKILL)))

        return failure, deadline, child, UnreviewedProcess

    def test_child_evidence_keeps_parent_identity_and_projects_only_reviewed_process_facts(self):
        (failure, deadline, child, UnreviewedProcess) = self._child_exchange_failure()
        relationships = dict(owned_relationships(failure))

        self.assertIs(deadline, relationships["transport primary"])
        self.assertIs(child, relationships["model HTTP child evidence"])
        details = owned_details(failure)

        self.assertEqual(
            {
                "message": str(failure),
                "model_http_pid": 1234,
                "model_http_returncode": -int(signal.SIGKILL),
                "model_http_signals_sent": 2,
                "model_http_last_signal": int(signal.SIGKILL),
            },
            details,
        )

        evidence = failure_evidence(failure, secrets=())

        child_evidence = next(
            item["failure"] for item in evidence["relationships"]
            if item["relationship"] == "model HTTP child evidence"
        )

        self.assertEqual(503, child_evidence["status"])
        self.assertIsNone(child_evidence["request_id"])
        failure.process = UnreviewedProcess()

        self.assertEqual({"message": str(failure)}, owned_details(failure))

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    def test_child_projection_omits_text_and_headers(self):
        (failure, _deadline, _child, _process_type) = self._child_exchange_failure()

        self.assertNotIn("PRIVATE_CHILD", repr(failure_evidence(failure, secrets=())))
