"""Installed test failures end with a searchable, bounded result index."""

import io
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
