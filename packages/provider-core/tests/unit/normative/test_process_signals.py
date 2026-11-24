"""Termination requests reach the owner without replacing its cleanup policy."""

import asyncio
import signal
import unittest
from unittest.mock import Mock, patch

from provider_core.process_signals import termination_signals


class ProcessSignalsTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_signals_reach_stop_policy_and_restore_original_handlers(self):
        original = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        stopped = asyncio.Event()

        for number in original:
            stopped.clear()

            with termination_signals(stopped.set):
                signal.raise_signal(number)
                await asyncio.wait_for(stopped.wait(), timeout=1)

            self.assertEqual({number: signal.getsignal(number) for number in original}, original)

    async def test_installation_failure_restores_already_acquired_signal(self):
        failure = OSError("Cannot install the second signal handler")
        previous = Mock()

        with patch("provider_core.process_signals.signal.signal", side_effect=[previous, failure, None]) as install:
            with self.assertRaises(OSError) as caught:
                with termination_signals(Mock()):
                    self.fail("The service must not run after partial signal setup")

        self.assertIs(caught.exception, failure)
        self.assertEqual(install.call_args.args, (signal.SIGINT, previous))

    async def test_cleanup_attempts_both_restorations_and_preserves_original_failure(self):
        operation = RuntimeError("Service failed")
        restoration = OSError("Cannot restore SIGTERM")
        previous = Mock()

        with patch("provider_core.process_signals.signal.signal", side_effect=[previous, previous, restoration, None]) as install:
            with self.assertRaises(BaseExceptionGroup) as caught:
                with termination_signals(Mock()):
                    raise operation

        self.assertEqual(caught.exception.exceptions, (operation, restoration))
        self.assertEqual(install.call_args.args, (signal.SIGINT, previous))
