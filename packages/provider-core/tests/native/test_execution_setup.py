"""Common startup settings preserve explicit selection and session custody."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from provider_core.execution_setup import ExecutionSettings, parse_resource_requirements
from provider_core.runner_session import RunnerSession


def _configuration():
    return {
        "backend": "thread",
        "host": dict(input_bytes=4096, result_bytes=4096, state_values=2,
                     progress_records=1, initialization_seconds=2, retirement_seconds=2),
        "operation_policy": dict(queue_stop_request_seconds=2, execution_stop_request_seconds=2,
                                 cleanup_overdue_flag_seconds=2, reject_encoded_input_over_bytes=1024,
                                 reject_encoded_result_over_bytes=1024),
        "resource_requirement": [dict(kind="cpu", amount=1, unit="thread", selector="generic", exclusive=False)],
    }


class ExecutionSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_snapshot_is_independent_of_the_source_document(self):
        configuration = _configuration()
        before = deepcopy(configuration)
        settings = ExecutionSettings(configuration)
        configuration["host"]["input_bytes"] = 1
        configuration["resource_requirement"].clear()
        self.assertEqual(settings.host, before["host"])
        self.assertEqual(settings.resource_requirements, parse_resource_requirements(before["resource_requirement"]))
        with self.assertRaises(TypeError):
            settings.host["input_bytes"] = 1

    def test_backend_is_explicit_and_supported(self):
        configuration = _configuration()
        del configuration["backend"]
        with self.assertRaises(KeyError):
            ExecutionSettings(configuration)
        for backend, error in (("automatic", ValueError), (True, TypeError)):
            configuration["backend"] = backend
            with self.subTest(backend=backend), self.assertRaises(error):
                ExecutionSettings(configuration)

    async def test_refusals_leave_native_session_with_caller_and_success_does_not_connect(self):
        with tempfile.TemporaryDirectory() as root:
            session = RunnerSession(Path(root) / "unused.sock", role="science", expected_lock_identity=(0, 0),
                                    startup_seconds=2, retirement_seconds=2)
            self.addCleanup(session.close)
            descriptor = session.control.fileno()
            configuration = _configuration()
            with self.assertRaises(ValueError):
                ExecutionSettings(configuration).create_host(process_session=session)
            configuration["backend"] = "process"
            settings = ExecutionSettings(configuration)
            with self.assertRaises(ValueError):
                settings.create_host()
            invalid = dict(settings.host, state_values=0)
            with self.assertRaises(ValueError):
                settings.create_host(process_session=session, host=invalid)
            self.assertEqual(descriptor, session.control.fileno())
            self.assertFalse(session.control_connected)
            async with settings.create_host(process_session=session):
                self.assertFalse(session.control_connected)
            self.assertEqual(-1, session.control.fileno())

    async def test_thread_host_accepts_provider_capacity_override_without_changing_settings(self):
        settings = ExecutionSettings(_configuration())
        async with settings.create_host(host=dict(settings.host, state_values=1)) as host:
            self.assertEqual(1, host.state_values)
            self.assertEqual(2, settings.host["state_values"])

    def test_thread_inventory_requires_no_sessions_including_an_empty_mapping(self):
        settings = ExecutionSettings(_configuration())
        settings.require_sessions(process_sessions=None, expected_keys={"baseline", "peaks"})
        for sessions in ({}, {"baseline": object()}):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                settings.require_sessions(process_sessions=sessions, expected_keys={"baseline", "peaks"})

    def test_process_inventory_refusal_preserves_every_callers_session(self):
        configuration = _configuration()
        configuration["backend"] = "process"
        settings = ExecutionSettings(configuration)
        with tempfile.TemporaryDirectory() as root:
            sessions = [RunnerSession(Path(root) / f"unused-{index}.sock", role="science",
                                      expected_lock_identity=(0, 0), startup_seconds=2, retirement_seconds=2)
                        for index in range(2)]
            for session in sessions:
                self.addCleanup(session.close)
            first, second = sessions
            descriptors = [session.control.fileno() for session in sessions]
            for supplied in (None, {}, {"baseline": first}, {"baseline": first, "peaks": second, "extra": first},
                             {"baseline": first, "peaks": first}):
                with self.subTest(keys=None if supplied is None else tuple(supplied)):
                    with self.assertRaises(ValueError):
                        settings.require_sessions(process_sessions=supplied, expected_keys={"baseline", "peaks"})
                    self.assertEqual(descriptors, [session.control.fileno() for session in sessions])
                    self.assertTrue(all(not session.control_connected for session in sessions))
                    self.assertTrue(all(session.retirement is None for session in sessions))

            # Keys express provider topology, including model-family/index pairs.
            for keys in (("baseline", "peaks"), (("hf", 0), ("chf", 0))):
                inventory = dict(zip(keys, sessions, strict=True))
                settings.require_sessions(process_sessions=inventory, expected_keys=keys)
                self.assertEqual(descriptors, [session.control.fileno() for session in sessions])
                self.assertTrue(all(not session.control_connected for session in sessions))
                self.assertTrue(all(session.retirement is None for session in sessions))
