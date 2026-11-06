"""The original lifecycle failure has an age independent of current claim state."""

import asyncio
import errno
import json
import unittest
from unittest.mock import Mock

from provider_core.controller import ControllerFailed
from provider_core.diagnostics import failure_evidence
from provider_core.execution import ExecutionFailed
from provider_core.ownership import AttemptClaims
from tests.fakes.api import Transport, api
from tests.fakes.attempt_store import record
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.controller import lifecycle_replies
from tests.fakes.execution import LiveAttemptProbe, Session, live_controller


class ExecutionFailureAgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_failure_ages_while_cleanup_is_held_and_remains_historical_after_release(self):
        loop = asyncio.get_running_loop()
        probe, claims, delivery = LiveAttemptProbe(), AttemptClaims.live(), RecordingDelivery()
        session = Session(probe)
        original_cleanup_failure = OSError(errno.EBUSY, "PRIVATE_SCIENTIFIC_RESOURCE")
        session.retirement_failure = original_cleanup_failure
        controller = live_controller(
            api(Transport(*lifecycle_replies()), probe.scope.api_origin), probe, claims, session,
            delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
            delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=1, task_settle_seconds=0.1,
        )
        selected = record()
        try:
            with self.assertRaises(ControllerFailed) as first:
                await controller.run(selected)
            original = first.exception.cause
            self.assertIs(type(original), ExecutionFailed)
            self.assertIs(original, controller.operation.last_failure)
            self.assertIs(original.cause, original_cleanup_failure)
            self.assertEqual(1, len(claims.live_owners()))
            self.assertFalse(controller.shutdown_ready)
            first_at = loop.time()
            first_evidence = failure_evidence(original, secrets=())

            await asyncio.sleep(0.04)
            with self.assertRaises(ControllerFailed) as second:
                await controller.settle()
            self.assertIs(second.exception, first.exception)
            self.assertIs(second.exception.cause, original)
            self.assertEqual(1, len(claims.live_owners()))
            second_at = loop.time()
            second_evidence = failure_evidence(original, secrets=())
        finally:
            session.retirement_failure = None
            await controller.settle()

        self.assertTrue(controller.shutdown_ready)
        self.assertEqual(0, len(claims.live_owners()))
        self.assertEqual(1, len(delivery.items))
        self.assertEqual(["prepare", "execute"], [event[0] for event in session.events if event[0] != "retire"])
        self.assertIs(controller.operation.last_failure, original)
        released_evidence = failure_evidence(original, secrets=())
        snapshots = (first_evidence, second_evidence, released_evidence)
        evidence = json.dumps(snapshots, sort_keys=True)
        for snapshot in snapshots:
            self.assertEqual(selected.input.job_ref, snapshot["job_ref"], msg=evidence)
            self.assertEqual(ATTEMPT_REF, snapshot["execution_attempt_ref"], msg=evidence)
            self.assertTrue(snapshot["delivery_acceptance_received"], msg=evidence)
            self.assertIn("failure_age_seconds", snapshot,
                          msg="The diagnostic identifies the Job and Attempt but not the age of its original lifecycle failure: " + evidence)
            self.assertGreaterEqual(snapshot["failure_age_seconds"], 0, msg=evidence)
        elapsed = second_at - first_at
        observed_age = second_evidence["failure_age_seconds"] - first_evidence["failure_age_seconds"]
        self.assertAlmostEqual(elapsed, observed_age, delta=0.01, msg=evidence)
        self.assertGreater(second_evidence["failure_age_seconds"], first_evidence["failure_age_seconds"], msg=evidence)
        # Retained evidence still describes the old failure after release. Its
        # increasing age does not claim that the scientific resource is held.
        self.assertGreaterEqual(released_evidence["failure_age_seconds"], second_evidence["failure_age_seconds"], msg=evidence)
