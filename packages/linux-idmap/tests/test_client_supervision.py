"""The client owns a private process group and preserves every cleanup failure."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from linux_idmap import _docker as docker
from linux_idmap._docker import DockerError


class ClientSupervisionTests(unittest.TestCase):
    def test_closed_pipe_descendants_are_stopped_before_leader_reaping(self):
        cases = ((0, False, None), (9, False, 'docker_command'), (0, True, 'invalid_response'))
        for status, invalid_utf8, category in cases:
            with self.subTest(status=status, invalid_utf8=invalid_utf8), tempfile.TemporaryDirectory() as directory:
                pidfile = Path(directory) / 'descendant'
                token = 'idmap-private-client-' + Path(directory).name
                program = (
                    'import os,pathlib,subprocess,sys; '
                    f'child=subprocess.Popen([sys.executable,"-I","-S","-c","import time;time.sleep(5)",{token!r}],'
                    'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                    f'pathlib.Path({str(pidfile)!r}).write_text(str(child.pid)); '
                    f'os.write(1, {bytes([255]) if invalid_utf8 else b""!r}); sys.exit({status})'
                )
                signal_group = os.killpg
                def kill_before_reap(pid, number):
                    info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    self.assertIsNotNone(info, 'the leader must remain waitable until group signaling')
                    signal_group(pid, number)
                try:
                    with patch('linux_idmap._docker.os.killpg', side_effect=kill_before_reap) as kill:
                        arguments = [sys.executable, '-I', '-S', '-B', '-c', program]
                        if category is None:
                            self.assertEqual('', docker._run_command(arguments, env=os.environ, timeout=3))
                        else:
                            with self.assertRaises(DockerError) as caught:
                                docker._run_command(arguments, env=os.environ, timeout=3)
                            self.assertEqual(category, caught.exception.category)
                    self.assertEqual(1, kill.call_count)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(kill.call_args.args[0], os.WNOHANG)
                    pid = int(pidfile.read_text())
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            state = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b') ', 1)[1][:1]
                        except (FileNotFoundError, ProcessLookupError):
                            break
                        if state in (b'Z', b'X', b'x'):
                            break
                        time.sleep(.01)
                    else:
                        self.fail('ordinary descendant remained live after client retirement')
                finally:
                    if pidfile.exists():
                        pid = int(pidfile.read_text())
                        try:
                            if token.encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0'):
                                os.kill(pid, signal.SIGKILL)
                        except (FileNotFoundError, ProcessLookupError):
                            pass

    def test_finite_descendant_output_is_drained_after_its_parent_exits(self):
        program = ('import subprocess,sys; '
                   'subprocess.Popen([sys.executable,"-I","-S","-B","-c",'
                   '"import time;time.sleep(.05);print(\'late output\')"])')
        result = docker._run_command([sys.executable, '-I', '-S', '-B', '-c', program],
                                     env=os.environ, timeout=3)
        self.assertEqual('late output\n', result)

    def test_cancellation_wins_over_buffered_output_and_observed_exit(self):
        import threading
        cancellation = threading.Event()
        process, selector = Mock(), Mock()
        selector.get_map.return_value = {1: object()}
        def ready(_timeout):
            cancellation.set()
            return [(SimpleNamespace(fd=1, fileobj=process.stdout), 1)]
        selector.select.side_effect = ready
        with patch('linux_idmap._docker.subprocess.Popen', return_value=process), \
             patch('linux_idmap._docker.selectors.DefaultSelector', return_value=selector), \
             patch('linux_idmap._docker.os.set_blocking'), \
             patch('linux_idmap._docker.os.read') as read, \
             patch('linux_idmap._docker._peek_exit', return_value=SimpleNamespace(si_code=os.CLD_EXITED, si_status=0)), \
             patch('linux_idmap._docker._terminate') as retire:
            with self.assertRaises(DockerError) as caught:
                docker._run_command(['fixture'], env={}, timeout=1, cancel=cancellation, mutation=True)
        self.assertEqual('cancelled', caught.exception.category)
        read.assert_not_called()
        retire.assert_called_once_with(process)

    def test_every_client_resource_closes_without_masking_prior_failures(self):
        operation_cleanup = DockerError('client_cleanup', 'group cleanup reported failure')
        selector_error, stdout_error, stderr_error = (OSError(name) for name in ('selector', 'stdout', 'stderr'))
        selector_factory, spawn, terminate = docker.selectors.DefaultSelector, subprocess.Popen, docker._terminate
        class ClosingResource:
            def __init__(self, resource, error):
                self.resource, self.error = resource, error
            def __getattr__(self, name):
                return getattr(self.resource, name)
            def close(self):
                self.resource.close()
                raise self.error
        selector = selector_factory()
        def start(*args, **kwargs):
            process = spawn(*args, **kwargs)
            process.stdout = ClosingResource(process.stdout, stdout_error)
            process.stderr = ClosingResource(process.stderr, stderr_error)
            return process
        def retire(process):
            terminate(process)
            raise operation_cleanup
        with patch('linux_idmap._docker.selectors.DefaultSelector',
                   return_value=ClosingResource(selector, selector_error)), \
             patch('linux_idmap._docker.subprocess.Popen', side_effect=start), \
             patch('linux_idmap._docker._terminate', side_effect=retire) as retirement:
            with self.assertRaises(ExceptionGroup) as caught:
                docker._run_command([sys.executable, '-I', '-S', '-B', '-c', 'raise SystemExit(3)'],
                                    env=os.environ, timeout=3, mutation=True)
        failures = caught.exception.exceptions
        self.assertEqual('docker_command', failures[0].category)
        self.assertEqual((operation_cleanup, selector_error, stdout_error, stderr_error), failures[1:])
        self.assertTrue(caught.exception.mutation_uncertain)
        retirement.assert_called_once()
        process = retirement.call_args.args[0]
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertIsNotNone(process.returncode)

    def test_spawn_failure_and_selector_close_interruption_are_both_preserved(self):
        spawn_error, close_error = PermissionError('PRIVATE spawn'), KeyboardInterrupt()
        selector = Mock()
        selector.close.side_effect = close_error
        with patch('linux_idmap._docker.selectors.DefaultSelector', return_value=selector), \
             patch('linux_idmap._docker.subprocess.Popen', side_effect=spawn_error), \
             patch('linux_idmap._docker._terminate') as retirement:
            with self.assertRaises(BaseExceptionGroup) as caught:
                docker._run_command(['fixture'], env={}, timeout=1, mutation=True)
        operation, cleanup = caught.exception.exceptions
        self.assertEqual('docker_unavailable', operation.category)
        self.assertIs(spawn_error, operation.__cause__)
        self.assertIs(close_error, cleanup)
        self.assertTrue(caught.exception.mutation_uncertain)
        self.assertNotIsInstance(caught.exception, Exception)
        retirement.assert_not_called()
        selector.close.assert_called_once()

    def test_interrupted_spawn_cannot_be_marked_known_not_dispatched(self):
        for operation in (KeyboardInterrupt(), ValueError('unknown spawn stage')):
            with self.subTest(operation=type(operation).__name__):
                cleanup = OSError('PRIVATE selector close')
                selector = Mock()
                selector.close.side_effect = cleanup
                with patch('linux_idmap._docker.selectors.DefaultSelector', return_value=selector), \
                     patch('linux_idmap._docker.subprocess.Popen', side_effect=operation):
                    with self.assertRaises(BaseExceptionGroup) as caught:
                        docker._run_command(['fixture'], env={}, timeout=1, mutation=True)
                self.assertEqual((operation, cleanup), caught.exception.exceptions)
                self.assertTrue(caught.exception.mutation_uncertain)

    def test_spawn_cannot_forward_foreign_known_not_dispatched_metadata(self):
        for original in (DockerError('fixture', 'unknown spawn stage'),
                         ExceptionGroup('foreign spawn group', [ValueError('unknown stage')])):
            with self.subTest(original=type(original).__name__):
                original.mutation_uncertain = False
                with patch('linux_idmap._docker.subprocess.Popen', side_effect=original):
                    with self.assertRaises(ExceptionGroup) as caught:
                        docker._run_command(['fixture'], env={}, timeout=1, mutation=True)
                self.assertIs(original, caught.exception.exceptions[0])
                self.assertFalse(original.mutation_uncertain)
                self.assertTrue(caught.exception.mutation_uncertain)

    def test_sole_pipe_close_failure_keeps_its_type_and_closes_later_pipe(self):
        error = OSError('PRIVATE pipe close')
        process, selector = Mock(), Mock()
        process.stdout.close.side_effect = error
        selector.get_map.return_value = {}
        exited = SimpleNamespace(si_code=os.CLD_EXITED, si_status=0)
        with patch('linux_idmap._docker.subprocess.Popen', return_value=process), \
             patch('linux_idmap._docker.selectors.DefaultSelector', return_value=selector), \
             patch('linux_idmap._docker.os.set_blocking'), \
             patch('linux_idmap._docker._peek_exit', return_value=exited), \
             patch('linux_idmap._docker._terminate') as retirement:
            with self.assertRaises(OSError) as caught:
                docker._run_command(['fixture'], env={}, timeout=1, mutation=True)
        self.assertIs(error, caught.exception)
        retirement.assert_called_once_with(process)
        selector.close.assert_called_once()
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_selector_allocation_failure_cannot_dispatch_a_client(self):
        allocation = OSError('PRIVATE selector allocation')
        with patch('linux_idmap._docker.selectors.DefaultSelector', side_effect=allocation), \
             patch('linux_idmap._docker.subprocess.Popen') as spawn, \
             patch('linux_idmap._docker._terminate') as retire:
            with self.assertRaises(DockerError) as caught:
                docker._run_command(['must-not-run'], env={}, timeout=1, mutation=True)
        self.assertEqual('client_unavailable', caught.exception.category)
        self.assertFalse(caught.exception.mutation_uncertain)
        self.assertIs(allocation, caught.exception.__cause__)
        self.assertNotIn('PRIVATE', str(caught.exception))
        spawn.assert_not_called()
        retire.assert_not_called()

    def test_admission_is_rechecked_after_selector_preparation(self):
        import threading
        cases = ((False, True, 'timeout'), (True, False, 'cancelled'), (True, True, 'cancelled'))
        for cancelled, expired, category in cases:
            for close_fails in (False, True):
                with self.subTest(cancelled=cancelled, expired=expired, close_fails=close_fails):
                    cancellation = threading.Event()
                    clock = [0.0]
                    selector = Mock()
                    cleanup = OSError('PRIVATE selector close')
                    if close_fails:
                        selector.close.side_effect = cleanup
                    def allocate():
                        if cancelled:
                            cancellation.set()
                        if expired:
                            clock[0] = 2.0
                        return selector
                    with patch('linux_idmap._docker.time.monotonic', side_effect=lambda: clock[0]), \
                         patch('linux_idmap._docker.selectors.DefaultSelector', side_effect=allocate), \
                         patch('linux_idmap._docker.subprocess.Popen') as spawn, \
                         patch('linux_idmap._docker._terminate') as retire:
                        with self.assertRaises((DockerError, ExceptionGroup)) as caught:
                            docker._run_command(['must-not-run'], env={}, timeout=1,
                                                cancel=cancellation, mutation=True)
                    failure = caught.exception
                    if close_fails:
                        operation, close_error = failure.exceptions
                        self.assertIs(cleanup, close_error)
                    else:
                        operation = failure
                    self.assertEqual(category, operation.category)
                    self.assertFalse(failure.mutation_uncertain)
                    spawn.assert_not_called()
                    retire.assert_not_called()
                    selector.close.assert_called_once()

    def test_sigchld_auto_reaping_is_rejected_before_dispatch(self):
        import linux_idmap
        source = str(Path(linux_idmap.__file__).parent.parent)
        program = '''
import signal, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from linux_idmap._docker import DockerError, _run_command
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
with patch('linux_idmap._docker.subprocess.Popen') as spawn:
    try:
        _run_command(['must-not-run'], env={}, timeout=1, mutation=True)
    except DockerError as error:
        assert error.category == 'invalid_policy'
        assert not error.mutation_uncertain
    else:
        raise AssertionError('auto-reaping was accepted')
    spawn.assert_not_called()
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', program, source],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lost_wait_ownership_never_signals_a_numeric_group(self):
        process = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'pass'])
        _, status = os.waitpid(process.pid, 0)  # A competing waiter consumed it.
        try:
            with patch('linux_idmap._docker.os.killpg') as kill:
                with self.assertRaises(DockerError) as caught:
                    docker._terminate(process)
            self.assertEqual('client_cleanup', caught.exception.category)
            kill.assert_not_called()
        finally:
            process.returncode = os.waitstatus_to_exitcode(status)

    def test_failed_group_signal_still_reaps_and_remains_a_failure(self):
        process = SimpleNamespace(pid=123, returncode=None)
        signal_error = PermissionError('private signal detail')
        with patch('linux_idmap._docker._peek_exit'), \
             patch('linux_idmap._docker.os.killpg', side_effect=signal_error), \
             patch('linux_idmap._docker._reap') as reap:
            with self.assertRaises(DockerError) as caught:
                docker._terminate(process)
        self.assertIs(signal_error, caught.exception.__cause__)
        self.assertEqual('client_cleanup', caught.exception.category)
        reap.assert_called_once_with(process)

    def test_signal_and_reap_failures_are_retained_in_order(self):
        signal_error = PermissionError('private signal detail')
        reap_error = DockerError('client_cleanup', 'child did not exit')
        with patch('linux_idmap._docker._peek_exit'), \
             patch('linux_idmap._docker.os.killpg', side_effect=signal_error), \
             patch('linux_idmap._docker._reap', side_effect=reap_error):
            with self.assertRaises(BaseExceptionGroup) as caught:
                docker._terminate(SimpleNamespace(pid=123))
        self.assertIs(signal_error, caught.exception.exceptions[0].__cause__)
        self.assertIs(reap_error, caught.exception.exceptions[1])

    def test_reap_has_a_bound_and_does_not_use_popens_echild_fallback(self):
        clock = [0.0]
        def elapse(delay):
            clock[0] += delay
        process = Mock(pid=123, returncode=None)
        with patch('linux_idmap._docker.os.waitpid', return_value=(0, 0)), \
             patch('linux_idmap._docker.time.monotonic', side_effect=lambda: clock[0]), \
             patch('linux_idmap._docker.time.sleep', side_effect=elapse):
            with self.assertRaises(DockerError) as caught:
                docker._reap(process)
        self.assertEqual('client_cleanup', caught.exception.category)
        self.assertEqual(1, clock[0])
        process.wait.assert_not_called()
        with patch('linux_idmap._docker.os.waitpid', side_effect=ChildProcessError):
            with self.assertRaises(DockerError):
                docker._reap(process)
        self.assertIsNone(process.returncode)

    def test_foreign_exception_is_not_mutated_when_cleanup_also_fails(self):
        class ForeignError(Exception):
            def __setattr__(self, name, value):
                raise AttributeError('foreign exception rejects attributes')

        terminate = docker._terminate
        cleanup = DockerError('client_cleanup', 'client retirement unconfirmed')
        def retire_then_report(process):
            terminate(process)
            raise cleanup
        for operation in (ForeignError('PRIVATE'), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(operation=type(operation).__name__):
                with patch('linux_idmap._docker.os.set_blocking', side_effect=operation), \
                     patch('linux_idmap._docker._terminate', side_effect=retire_then_report):
                    with self.assertRaises(BaseExceptionGroup) as caught:
                        docker._run_command([sys.executable, '-I', '-S', '-B', '-c', 'import time;time.sleep(5)'],
                                            env=os.environ, timeout=1, mutation=True)
                self.assertIs(operation, caught.exception.exceptions[0])
                self.assertIs(cleanup, caught.exception.exceptions[1])
                self.assertTrue(caught.exception.mutation_uncertain)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(isinstance(operation, Exception), isinstance(caught.exception, Exception))

    def test_interrupted_cleanup_preserves_cancellation_and_unconfirmed_retirement(self):
        interruption = KeyboardInterrupt()
        with patch('linux_idmap._docker._peek_exit'), \
             patch('linux_idmap._docker.os.killpg', side_effect=interruption), \
             patch('linux_idmap._docker._reap') as reap:
            with self.assertRaises(BaseExceptionGroup) as caught:
                docker._terminate(SimpleNamespace(pid=123))
        self.assertIs(interruption, caught.exception.exceptions[0])
        self.assertEqual('client_cleanup', caught.exception.exceptions[1].category)
        reap.assert_called_once()
        with patch('linux_idmap._docker.os.waitpid', side_effect=interruption):
            with self.assertRaises(BaseExceptionGroup) as caught:
                docker._reap(SimpleNamespace(pid=123))
        self.assertIs(interruption, caught.exception.exceptions[0])
        self.assertEqual('client_cleanup', caught.exception.exceptions[1].category)


if __name__ == '__main__':
    unittest.main()
