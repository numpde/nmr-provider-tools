"""Shutdown retains original owners, cancellation and rejected report bytes."""

import asyncio
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from provider_core.attempt_shutdown import drain_attempts
from provider_core.controller import ControllerFailed
from provider_core.delivery_client import DeliveryRejected
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import TerminalSelectedCheckpoint
from provider_core.task_boundary import contain_task_exit
from tests.fakes.api import Transport, api
from tests.fakes.attempt_store import record, scope
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.controller import SingleRetirementSession, lifecycle_replies
from tests.fakes.execution import LiveAttemptProbe, live_controller


async def retain_rejected_report(witness):
    probe = LiveAttemptProbe()
    claims = AttemptClaims.live()
    science = SingleRetirementSession(probe)
    delivery = RecordingDelivery()
    delivery.submit = AsyncMock(side_effect=DeliveryRejected("Fixture receipt does not match the selected report"))

    controller = live_controller(
        api(Transport(*lifecycle_replies()), scope().api_origin),
        probe,
        claims,
        science,
        delivery=delivery,
        on_delivery_retry=Mock(),
        delivery_retry_initial_seconds=0.01,
        delivery_retry_max_seconds=0.02,
        allowed_job_states=frozenset({"open"}),
        poll_seconds=1,
        task_settle_seconds=0.1,
    )

    try:
        await controller.run(record())
    except ControllerFailed as failure:
        assert failure is controller.last_failure
    else:
        raise AssertionError("Rejected report must leave the original controller failure")

    observed = asyncio.Event()

    shutdown = asyncio.create_task(
        drain_attempts((controller,), retry_seconds=0.01, report_failure=lambda _: observed.set()),
    )

    await observed.wait()

    witness.write_text(
        json.dumps(
            {
                "selected": isinstance(controller.operation._checkpoint, TerminalSelectedCheckpoint),
                "retained_body": controller.operation._delivery_outcome.body_sha256,
                "ready": controller.shutdown_ready,
                "claims": len(claims.live_owners()),
                "submissions": delivery.submit.await_count,
                "science": [event[0] for event in science.events],
            }
        ),
    )

    await shutdown


class GatedController:
    """Script only the cleanup port while retaining its real asynchronous waits."""

    def __init__(self, *, hold_join, failure):
        self.hold_join = hold_join
        self.failure = failure
        self.joined = asyncio.Event()
        self.release = asyncio.Event()
        self.settled_twice = asyncio.Event()
        self.shutdown_ready = False
        self.settlement_count = 0

    def request_stop(self):
        pass

    async def join(self):
        self.joined.set()

        if self.hold_join:
            await self.release.wait()

    async def settle_for_shutdown(self):
        self.settlement_count += 1

        if self.settlement_count >= 2:
            self.settled_twice.set()

        if self.release.is_set():
            self.shutdown_ready = True

            return

        raise self.failure


class AttemptShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_cleanup_and_preserves_original_failure(self):
        for hold_join in (True, False):
            with self.subTest(hold_join=hold_join):
                original = OSError("Fixture cleanup remains pending")
                owner = GatedController(hold_join=hold_join, failure=original)
                shutdown = asyncio.create_task(drain_attempts((owner,), retry_seconds=0.01))

                try:
                    await owner.joined.wait()
                    shutdown.cancel("Fixture caller stopped observing")
                    await asyncio.wait_for(owner.settled_twice.wait(), 2)
                    self.assertFalse(shutdown.done())
                    owner.release.set()

                    with self.assertRaises(BaseExceptionGroup) as caught:
                        await asyncio.wait_for(shutdown, 2)

                    self.assertIsNotNone(caught.exception.subgroup(asyncio.CancelledError))
                    self.assertIsNotNone(caught.exception.subgroup(lambda cause: cause is original))
                    self.assertTrue(owner.shutdown_ready)
                finally:
                    owner.release.set()
                    await asyncio.gather(shutdown, return_exceptions=True)

    async def test_one_fatal_cleanup_does_not_skip_an_independent_owner(self):
        fatal = SystemExit(17)
        first = GatedController(hold_join=False, failure=fatal)
        second = GatedController(hold_join=False, failure=RuntimeError("Unused fixture failure"))

        second.release.set()

        shutdown = asyncio.create_task(
            contain_task_exit(drain_attempts, (first, second), retry_seconds=0.01),
        )

        try:
            await asyncio.wait_for(first.settled_twice.wait(), 2)
            self.assertTrue(second.shutdown_ready)
            self.assertFalse(shutdown.done())
            first.release.set()

            # Use the SDK task boundary so a raw SystemExit cannot bypass the
            # parent observer and terminate asyncio's loop.
            result = await asyncio.gather(shutdown, return_exceptions=True)

            self.assertIsInstance(result[0], BaseExceptionGroup)
            self.assertIn(fatal, result[0].exceptions)
        finally:
            first.release.set()
            await asyncio.gather(shutdown, return_exceptions=True)

    def test_permanently_rejected_report_keeps_its_process_and_original_body(self):
        staged_root = pathlib.Path(__file__).resolve().parents[3]

        program = (
            "import asyncio, pathlib, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from tests.unit.adversarial.test_attempt_shutdown import retain_rejected_report\n"
            "asyncio.run(retain_rejected_report(pathlib.Path(sys.argv[2])))\n"
        )

        with tempfile.TemporaryDirectory() as temporary:
            witness = pathlib.Path(temporary) / "retained.json"

            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", program, str(staged_root), str(witness)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                import time

                deadline = time.monotonic() + 5

                while not witness.exists():
                    if (child.poll() is not None) or (time.monotonic() >= deadline):
                        self.fail("Qualification child did not retain its selected report")

                    time.sleep(0.01)

                evidence = json.loads(witness.read_text())

                self.assertTrue(evidence["selected"])
                self.assertTrue(evidence["retained_body"])
                self.assertFalse(evidence["ready"])
                self.assertEqual(evidence["claims"], 1)
                self.assertEqual(evidence["submissions"], 1)
                self.assertEqual(evidence["science"], ["prepare", "execute", "retire"])

                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=0.2)
            finally:
                # This case intentionally has no authorized delivery recovery.
                # Terminate only the owned test process after proving retention.
                child.kill()

                child.communicate(timeout=5)
