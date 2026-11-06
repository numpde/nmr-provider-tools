"""Installed test failures end with a searchable, bounded result index."""

import asyncio
import io
import logging
import unittest

from tests.run_suite import _FailureSummaryResult, _print_failure_summary


class RunnerSummaryTests(unittest.TestCase):
    def render(self, cases):
        output = io.StringIO()
        runner = unittest.TextTestRunner(stream=output, verbosity=0, resultclass=_FailureSummaryResult)
        result = runner.run(unittest.TestSuite(cases))
        _print_failure_summary(result, "integration", output)
        return result, output.getvalue()

    def test_names_causes_and_subtests_are_visible_at_the_end(self):
        class Cases(unittest.TestCase):
            def test_failed_assertion(self):
                self.fail("The restored Job failed to publish")

            def test_unexpected_exception(self):
                raise TimeoutError("The original delivery receipt did not arrive")

            def test_seeded_subtest(self):
                with self.subTest(mode="blocked", seed=23):
                    self.assertTrue(False, "No affected Job succeeded: " + ("x" * 1000))

        result, output = self.render((Cases("test_failed_assertion"),
                                      Cases("test_unexpected_exception"),
                                      Cases("test_seeded_subtest")))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(3, len(result.problems))
        summary = output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):]
        self.assertIn("3 failing cases", summary)
        for name in ("test_failed_assertion", "test_unexpected_exception", "test_seeded_subtest"):
            self.assertIn(name, summary)
        self.assertIn("mode='blocked'", summary)
        self.assertIn("seed=23", summary)
        self.assertIn("AssertionError: The restored Job failed to publish", summary)
        self.assertIn("TimeoutError: The original delivery receipt did not arrive", summary)
        self.assertIn("tests/unit/normative/test_test_runner_summary.py:", summary)
        self.assertNotIn("x" * 500, summary)
        self.assertIn("=== END PROVIDER-CORE FAILURE SUMMARY ===", summary)

    def test_success_has_no_failure_summary(self):
        class Passing(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)

        result, output = self.render((Passing("test_ok"),))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual([], result.problems)
        self.assertNotIn("FAILURE SUMMARY", output)

    def test_many_failures_are_bounded_and_counted(self):
        class Failing(unittest.TestCase):
            def test_failed(self):
                self.fail("A bounded failure")

        result, output = self.render(Failing("test_failed") for _ in range(32))
        summary = output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):]
        self.assertEqual(32, len(result.problems))
        self.assertIn("32 failing cases", summary)
        self.assertIn("7 additional failing cases", summary)
        self.assertEqual(25, summary.count("FAIL test_failed"))

    def test_unexpected_asyncio_loop_error_is_attributed_and_fails(self):
        class Cases(unittest.IsolatedAsyncioTestCase):
            async def test_reset(self):
                asyncio.get_running_loop().call_exception_handler({
                    "message": "Error on transport creation for incoming connection",
                    "exception": ConnectionResetError(),
                })

        result, output = self.render((Cases("test_reset"),))
        self.assertFalse(result.wasSuccessful(), msg=output)
        self.assertEqual(1, len(result.errors))
        self.assertEqual(1, len(result.problems))
        summary = output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):]
        self.assertIn("test_reset", summary)
        self.assertIn("ConnectionResetError", summary)
        self.assertIn("Error on transport creation", summary)

    def test_asyncio_error_during_runner_close_is_still_attributed(self):
        class Cases(unittest.IsolatedAsyncioTestCase):
            async def test_late_reset(self):
                async def pending():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        asyncio.get_running_loop().call_exception_handler({
                            "message": "Loop shutdown released a broken transport",
                            "exception": ConnectionResetError("late reset"),
                        })

                asyncio.create_task(pending())
                await asyncio.sleep(0)

        result, output = self.render((Cases("test_late_reset"),))
        self.assertFalse(result.wasSuccessful(), msg=output)
        self.assertEqual(1, len(result.errors))
        self.assertIn("test_late_reset", output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):])
        self.assertIn("late reset", output)

    def test_explicitly_asserted_asyncio_diagnostic_is_not_an_error(self):
        class Cases(unittest.IsolatedAsyncioTestCase):
            async def test_expected(self):
                with self.assertLogs("asyncio", level="ERROR") as captured:
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "Expected injected reset",
                        "exception": ConnectionResetError("intentionally unfinished TLS"),
                    })
                self.assertEqual(1, len(captured.records))
                self.assertIs(type(captured.records[0].exc_info[1]), ConnectionResetError)

        result, output = self.render((Cases("test_expected"),))
        self.assertTrue(result.wasSuccessful(), msg=output)
        self.assertEqual([], result.problems)
        self.assertNotIn("FAILURE SUMMARY", output)

    def test_plain_test_asyncio_error_is_also_recorded(self):
        class Cases(unittest.TestCase):
            def test_error_log(self):
                logging.getLogger("asyncio").error("Task exception was never retrieved")

        result, output = self.render((Cases("test_error_log"),))
        self.assertFalse(result.wasSuccessful(), msg=output)
        self.assertEqual(1, len(result.errors))
        self.assertIn("Task exception was never retrieved", output)
        self.assertIn("test_error_log", output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):])

    def test_asyncio_error_keeps_detailed_loop_context_outside_the_short_index(self):
        class Cases(unittest.TestCase):
            def test_context(self):
                logging.getLogger("asyncio").error(
                    chr(10).join((
                        "Error on transport creation for incoming connection",
                        "handle_traceback: deliberate test frame",
                        "protocol: test peer",
                        "transport: test SSL transport",
                    ))
                )

        result, output = self.render((Cases("test_context"),))
        self.assertFalse(result.wasSuccessful(), msg=output)
        self.assertEqual(1, len(result.errors))
        self.assertIn("handle_traceback: deliberate test frame", output)
        self.assertIn("protocol: test peer", output)
        self.assertIn("transport: test SSL transport", output)
        summary = output[output.index("=== PROVIDER-CORE FAILURE SUMMARY"):]
        self.assertNotIn("protocol: test peer", summary)
        self.assertNotIn("transport: test SSL transport", summary)

    def test_asyncio_observer_is_uninstalled_after_each_runner(self):
        logger = logging.getLogger("asyncio")
        before = tuple(logger.handlers)

        class Cases(unittest.IsolatedAsyncioTestCase):
            async def test_clean(self):
                await asyncio.sleep(0)

        result, output = self.render((Cases("test_clean"),))
        self.assertTrue(result.wasSuccessful(), msg=output)
        self.assertEqual(before, tuple(logger.handlers))

    def test_nested_runner_does_not_double_count_one_asyncio_error(self):
        def run_inner():
            class Inner(unittest.IsolatedAsyncioTestCase):
                async def test_error(self):
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "One nested loop error",
                        "exception": RuntimeError("inner"),
                    })

            return self.render((Inner("test_error"),))

        class Outer(unittest.TestCase):
            def test_nested(self):
                inner, _ = run_inner()
                self.assertEqual(1, len(inner.errors))
                self.assertFalse(inner.wasSuccessful())

        result, output = self.render((Outer("test_nested"),))
        self.assertTrue(result.wasSuccessful(), msg=output)
        self.assertEqual([], result.problems)
