"""Maximum exact terminal bodies survive queue restart without producer archives.

Receipt admission is exercised by the API/DeliveryWorker receiving tests. This
fixture proves local outstanding-body retention, not remote publication.
"""

import tempfile
import unittest
from pathlib import Path

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import complete_body, fail_body
from provider_core.delivery_store import DeliveryOutcome, DeliveryQueue, QueueLimits
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import ATTEMPT_REF


class TerminalQueueTests(unittest.TestCase):
    def test_maximum_completion_and_failure_requests_survive_queue_reopen_exactly(self):
        text = "\U0001f600" * 1024
        result = b"x" * 786432
        for operation in (TerminalOperation.COMPLETE, TerminalOperation.FAIL):
            if operation is TerminalOperation.COMPLETE:
                body = complete_body(ATTEMPT_REF, text, result)
                body += b" " * (2097152 - len(body))
                self.assertEqual(len(body), 2097152)
            else:
                body = fail_body(ATTEMPT_REF, "input_rejected", text)
                body += b" " * (16384 - len(body))
                self.assertEqual(len(body), 16384)
            item = DeliveryOutcome(scope().api_origin, scope().provider_ref, "job:example", ATTEMPT_REF, operation, body)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                queue = DeliveryQueue(root, limits=QueueLimits(1, 2097152, 1, 2097152))
                try:
                    accepted = queue.enqueue(item)
                    self.assertEqual(accepted, item.acceptance())
                finally:
                    queue.close()
                queue = DeliveryQueue(root, limits=QueueLimits(1, 2097152, 1, 2097152))
                try:
                    self.assertEqual(queue.next_pending(item.api_origin, item.provider_ref), item)
                    self.assertEqual(queue.enqueue(item), accepted)
                    row, = queue.inventory(item.api_origin, item.provider_ref)
                    self.assertEqual((row["body_sha256"], row["body_bytes"]), (item.body_sha256, len(body)))
                finally:
                    queue.close()
