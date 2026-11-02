"""A real signal can interrupt CPython after it has already created a child."""
import subprocess
import sys
import unittest

# Isolated signal/trace fixture; no process-global hooks enter the test runner.
# repo-check: allow-module-constant
PROGRAM = r'''
import builtins
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

sys.path.insert(0, sys.argv[1])
from linux_idmap._docker import DockerError, _run_command

child = None
error_type = getattr(builtins, sys.argv[2])

def interrupt(signum, frame):
    raise error_type('application timeout interrupted spawning')

def after_spawn(frame, event, arg):
    global child
    if (event == 'line' and frame.f_globals.get('__name__') == 'subprocess'
            and frame.f_code.co_name == '_execute_child'
            and getattr(frame.f_locals.get('self'), '_child_created', False)):
        # Keep the fixture's Popen alive solely so this test can retire it.
        # Production must not depend on these CPython internals.
        child = frame.f_locals['self']
        sys.settrace(None)
        signal.raise_signal(signal.SIGUSR1)
    return after_spawn

with tempfile.TemporaryDirectory() as directory:
    marker = Path(directory, 'executed')
    program = (
        'from pathlib import Path; import sys,time; '
        'Path(sys.argv[1]).write_text("executed"); time.sleep(.5)'
    )
    previous_handler = signal.signal(signal.SIGUSR1, interrupt)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(after_spawn)
        try:
            _run_command([sys.executable, '-I', '-S', '-B', '-c', program, str(marker)],
                         env=os.environ, timeout=2, mutation=True)
        except DockerError as error:
            observed = error
        else:
            raise AssertionError('signal did not interrupt Popen')
        finally:
            sys.settrace(previous_trace)
        assert child is not None, 'trace did not observe a created child'
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text() == 'executed':
                break
            time.sleep(.005)
        else:
            raise AssertionError('child execution was not established')
        assert isinstance(observed.__cause__, error_type)
        assert observed.mutation_uncertain, 'executed child was classified as not dispatched'
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGUSR1, previous_handler)
        if child is not None:
            # Preserve numeric group identity until signaling; reap exactly once.
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            finally:
                child.wait(timeout=2)
            try:
                os.waitpid(child.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                raise AssertionError('fixture child was not reaped')
'''

class SignalDispatchTests(unittest.TestCase):
    def test_signal_oserror_after_spawn_keeps_mutation_uncertain(self):
        import linux_idmap
        from pathlib import Path
        source = str(Path(linux_idmap.__file__).parent.parent)
        for error_name in ('OSError', 'TimeoutError'):
            with self.subTest(error_name=error_name):
                result = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', '-c', PROGRAM, source, error_name],
                    capture_output=True, text=True, timeout=8,
                )
                self.assertEqual(0, result.returncode, result.stderr)

if __name__ == '__main__':
    unittest.main()
