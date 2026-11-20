"""Use the installed matcher and native supervisor through either backend."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provider_core.contribution_errors import ScientificRejection
from provider_core.hosting import NotAdmitted, Outcome, ResultState, observe_cleanup
from provider_core.submission import Submission
from tests.fakes.runner import make_scientific_session

from reference_provider.application import run_queries
from reference_provider.binding import ReferenceMatcher
from reference_provider import provider as provider_setup
from reference_provider.science import VectorInput


class ReferenceBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"
        shutil.copyfile(Path(__file__).parent.parent / "config/deployments/localhost/provider.toml", self.configuration)
        self.enterContext(patch.object(provider_setup, "PROVIDER_CONFIG_PATH", self.configuration))
        self.original_config = self.configuration.read_text()

    def configure(self, backend):
        self.configuration.write_text(self.original_config.replace('backend = "thread"', f'backend = "{backend}"'))
        if backend == "thread":
            return None
        _, session = make_scientific_session(self)
        return session

    async def test_same_request_sequence_delivers_both_reports_and_retires_the_worker(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                session = self.configure(backend)
                received = []

                async def receive(payload):
                    received.append(json.loads(payload))

                async with provider_setup.open_provider(process_session=session) as provider:
                    host = provider.execution_host
                    if session is not None:
                        worker = host.process_id
                        self.assertNotEqual(os.getpid(), worker)
                    await run_queries(
                        provider=provider,
                        candidates=(VectorInput(values=(3, 4, 0, 0)), VectorInput(values=(0, 0, 1, 1))),
                        receive=receive,
                    )
                    self.assertEqual(["pattern-a", "pattern-b"], [report["label"] for report in received])
                    if session is not None:
                        self.assertEqual(worker, host.process_id)
                        self.assertIsNone(session.retirement)
                if session is not None:
                    self.assertEqual(0, session.retirement.returncode)
                    self.assertTrue(session.retirement.scratch_released)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(worker, 0)

    async def test_original_request_survives_cancelled_wait_and_prior_result_survives_rejection(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                session = self.configure(backend)
                async with provider_setup.open_provider(process_session=session) as provider:
                    host, matcher = provider.execution_host, provider.matcher
                    call = matcher.call(method=ReferenceMatcher.match)
                    analysis = host.open_analysis(allocations=frozenset({matcher.allocation}))
                    reservation = analysis.reservation(allocation=matcher.allocation)
                    request = Submission.from_call(
                        analysis_executor=analysis, call_binding=call,
                        scientific_input=VectorInput(values=(3, 4, 0, 0)), states={},
                        reservation_ref=reservation, operation_policy=provider.operation_policy,
                    )
                    operation = request.submit()
                    self.assertNotIsInstance(operation, NotAdmitted)
                    observing = asyncio.Event()
                    original_observe = operation.observe

                    async def interrupted_observation(**options):
                        observing.set()
                        await asyncio.Event().wait()
                        return await original_observe(**options)

                    with patch.object(operation, "observe", side_effect=interrupted_observation):
                        waiter = asyncio.create_task(request.wait())
                        await observing.wait()
                        waiter.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await waiter
                    self.assertIs(operation, request.operation)
                    self.assertIs(operation, request.submit())
                    completed = await request.wait()
                    payload = await operation.read_result(completed.require_result())
                    await observe_cleanup(analysis.close())

                    later = host.open_analysis(allocations=frozenset({matcher.allocation}))
                    later_reservation = later.reservation(allocation=matcher.allocation)
                    rejection = Submission.from_call(
                        analysis_executor=later, call_binding=call,
                        scientific_input=VectorInput(values=(0, 0, 0, 0)), states={},
                        reservation_ref=later_reservation, operation_policy=provider.operation_policy,
                    )
                    rejection.submit()
                    rejected = await rejection.wait()
                    self.assertIs(Outcome.REJECTED, rejected.outcome)
                    await observe_cleanup(later.close())
                    self.assertEqual(payload, await operation.read_result(completed.result))
                    await observe_cleanup(operation.release_result(completed.result))
                    observed = await operation.observe(after=None, wait_seconds=0)
                    self.assertIs(ResultState.RELEASED, observed.result_state)

    async def test_later_rejection_still_delivers_the_earlier_report(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                session = self.configure(backend)
                received = []

                async def receive(payload):
                    received.append(json.loads(payload)["label"])

                async with provider_setup.open_provider(process_session=session) as provider:
                    with self.assertRaises(ScientificRejection):
                        await run_queries(
                            provider=provider,
                            candidates=(VectorInput(values=(3, 4, 0, 0)), VectorInput(values=(0, 0, 0, 0))),
                            receive=receive,
                        )
                    self.assertEqual(["pattern-a"], received)

    async def test_process_startup_requires_an_explicit_native_owner(self):
        self.configuration.write_text(self.original_config.replace('backend = "thread"', 'backend = "process"'))
        with self.assertRaisesRegex(ValueError, "deployment-owned scientific runner session"):
            async with provider_setup.open_provider():
                self.fail("Process startup must require a native runner session")
