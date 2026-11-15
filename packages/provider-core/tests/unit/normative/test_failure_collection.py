"""Preserve original errors while explicit later phases still run."""

import asyncio
from contextlib import asynccontextmanager
import unittest

from provider_core.failure_collection import FailureCollector


class FailureCollectionTests(unittest.TestCase):
    def setUp(self):
        self.failures = FailureCollector(message="Independent request phases failed")

    def test_success_has_no_failure(self):
        with self.failures.capture():
            pass

        self.failures.raise_if_any()

    def test_single_cause_remains_the_original_exception(self):
        original = OSError("Receiver rejected the report")

        with self.failures.capture():
            raise original

        with self.assertRaises(expected_exception=OSError) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_failure_does_not_skip_later_phases(self):
        attempted = []
        originals = [OSError("Stop request failed"), OSError("Cleanup request failed")]

        for original in originals:
            with self.failures.capture():
                attempted.append(original)
                raise original

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            self.failures.raise_if_any()

        self.assertEqual(first=originals, second=attempted)
        self.assertEqual(first=tuple(originals), second=raised.exception.exceptions)

    def test_same_cause_reobserved_is_not_an_independent_failure(self):
        original = OSError("Receiver rejected the report")

        for _ in range(2):
            with self.failures.capture():
                raise original

        with self.assertRaises(expected_exception=OSError) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_equal_messages_do_not_merge_distinct_causes(self):
        originals = [OSError("Unavailable"), OSError("Unavailable")]

        for original in originals:
            with self.failures.capture():
                raise original

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=originals[0], expr2=raised.exception.exceptions[0])
        self.assertIs(expr1=originals[1], expr2=raised.exception.exceptions[1])

    def test_original_group_is_preserved_when_no_cause_was_recorded(self):
        original = ExceptionGroup("Original independent failures", [OSError("Read"), ValueError("Decode")])

        with self.failures.capture():
            raise original

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_reobserved_group_does_not_repeat_its_original_causes(self):
        original = ExceptionGroup("Original independent failures", [OSError("Read"), ValueError("Decode")])

        for _ in range(2):
            with self.failures.capture():
                raise original

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_settlement_group_keeps_new_cancellation_without_repeating_body_error(self):
        original = OSError("Receiver failed")
        interrupted = asyncio.CancelledError("Settlement waiter interrupted")
        settlement = BaseExceptionGroup("Settlement failure and interruption", [original, interrupted])

        settlement.add_note("Original settlement diagnostic")

        with self.failures.capture():
            raise original

        with self.failures.capture():
            raise settlement

        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            self.failures.raise_if_any()

        (body, remaining) = raised.exception.exceptions

        self.assertIs(expr1=original, expr2=body)
        self.assertEqual(first=(interrupted,), second=remaining.exceptions)
        self.assertEqual(first=settlement.__notes__, second=remaining.__notes__)
        self.assertIs(expr1=settlement.__traceback__, expr2=remaining.__traceback__)

    def test_captured_group_then_single_child_does_not_repeat_that_child(self):
        child = OSError("Receiver failed")
        original = ExceptionGroup("Request failures", [child, ValueError("Other resource")])

        with self.failures.capture():
            raise original

        with self.failures.capture():
            raise child

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_single_cancellation_is_propagated_without_reclassification(self):
        original = asyncio.CancelledError("Caller stopped waiting")

        with self.failures.capture():
            raise original

        with self.assertRaises(expected_exception=asyncio.CancelledError) as raised:
            self.failures.raise_if_any()

        self.assertIs(expr1=original, expr2=raised.exception)


class AsyncFailureCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_body_and_cleanup_are_reported_once_in_new_aggregate(self):
        original = OSError("Prediction failed")
        cleanup = ValueError("Cleanup failed independently")
        failures = FailureCollector(message="Request and cleanup failures")

        @asynccontextmanager
        async def request():
            with failures.capture():
                yield

            with failures.capture():
                raise cleanup

            failures.raise_if_any()

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            async with request():
                raise original

        self.assertEqual(first=(original, cleanup), second=raised.exception.exceptions)
        self.assertTrue(expr=raised.exception.__suppress_context__)
