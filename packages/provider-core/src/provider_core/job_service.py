"""Run a serial API job service around one persistent scientific process.

AttemptController owns START, phase observation, scientific permission and durable
result handoff. This service selects work, announces availability, and keeps those
owners alive through shutdown. Providers supply their scientific AnalysisSession.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass
import logging
import secrets

from provider_core.adapters.nmr.v1.attempt_request import start_body
from provider_core.api_errors import ApiFailure
from provider_core.attempt_record import RecoveryScope, StartPending
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.execution import ConditionRecovery, ObservationRecovery
from provider_core.failure_collection import FailureCollector
from provider_core.hello_publisher import HelloPublisher
from provider_core.ownership import AttemptClaims, LiveAttemptLimits
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.tools import require_positive_number


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobServicePolicy:
    """Polling, recovery and settlement intervals in seconds."""

    feed_seconds: float
    hello_seconds: float
    observation_seconds: float
    recovery_seconds: float
    retry_max_seconds: float
    settle_seconds: float

    def __post_init__(self):
        """Reject intervals that cannot bound a polling or recovery step."""

        for seconds in vars(self).values():
            require_positive_number(seconds)


class JobService:
    """Run one scientific job at a time using owned Attempts and delivery."""

    def __init__(
        self,
        *,
        provider_ref,
        consumer_id,
        generation,
        offering,
        display_name,
        description,
        host,
        policy,
        new_session,
        new_api,
        new_delivery,
        stopping,
    ):
        """Retain the API clients and Attempt claims for this provider lifetime."""
        self.offering = offering
        self.display_name = display_name
        self.description = description
        self.host = host
        self.new_session = new_session
        self.policy = policy
        self.new_api = new_api
        self.new_delivery = new_delivery
        self.api = new_api()
        self.publisher = HelloPublisher(
            new_api(), provider_ref=provider_ref, display_name=display_name,
            description=description, offerings=lambda: (self.offering,), stopping=stopping,
        )
        self.scope = RecoveryScope(self.api.origin, provider_ref, consumer_id, generation)
        self.stopping = stopping
        self.claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 262144, 131072))
        self.active = None

    async def serve(self):
        """Announce loaded science, consume jobs, then settle every acquired owner."""
        failures = FailureCollector(message="Provider service and shutdown failed")

        with failures.capture():
            if self.stopping.is_set():
                return

            await self.publisher.refresh()

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.jobs())
                tasks.create_task(self.publisher.run(
                    refresh_seconds=self.policy.hello_seconds, poll_seconds=self.policy.feed_seconds,
                ))
                tasks.create_task(self.watch_worker())

        self.stopping.set()

        if self.active is not None:
            self.active.request_stop()

            with failures.capture():
                await self.active.join()

            # The controller retains the original result while shared delivery is
            # unavailable. Never replace it with another calculation or drop it
            while not self.active.shutdown_ready:
                try:
                    await self.active.settle()
                except Exception:
                    log.exception("Attempt settlement failed; retaining its original work and result")
                    await asyncio.sleep(self.policy.retry_max_seconds)

        with failures.capture():
            await self.api.retire(owner=self)

        with failures.capture():
            await self.publisher.withdraw()

        with failures.capture():
            await self.publisher.retire()

        failures.raise_if_any()

    async def jobs(self):
        """Select only jobs for which this provider has no existing Attempt."""
        cursor = None

        while not self.stopping.is_set():
            try:
                page = await self.api.jobs(
                    analysis_kind_ref=self.offering.analysis_kind_ref,
                    has_provider_execution_attempt=False,
                    limit=50,
                    cursor=cursor,
                    owner=self,
                )
            except ApiFailure as error:
                await self.api.retire(owner=self)

                if not self.api.can_retry_job_discovery(error):
                    raise

                log.warning(f"Job discovery failed; {error=}")
                retry_seconds = self.policy.feed_seconds

                if error.retry_after_seconds is not None:
                    retry_seconds = max(retry_seconds, error.retry_after_seconds)

                await self.pause(retry_seconds)
                continue

            cursor = page.next_cursor

            for job in page.jobs:
                if self.stopping.is_set():
                    return

                if self.claims.owns_job(self.scope, job.input.job_ref):
                    continue

                await self.run_job(job.input)

            if cursor is None:
                await self.pause(self.policy.feed_seconds)

    async def run_job(self, job_input):
        """The SDK confirms START before invoking this session's inference call."""

        pending = StartPending(
            self.scope,
            self.offering.analysis_kind_ref,
            job_input,
            start_body(job_input.job_ref, self.scope.consumer_id + ":" + secrets.token_hex(32)),
        )

        self.active = AttemptController.live(
            self.new_api(),
            self.claims,
            self.new_session(),
            scope=self.scope,
            stopping=self.stopping,
            start_policy=LiveStartPolicy(self.policy.feed_seconds, self.retry_notice, self.recovery_notice),
            maximum_terminal_bytes=131072,
            delivery=self.new_delivery(),
            on_delivery_retry=self.retry_notice,
            delivery_retry_initial_seconds=self.policy.feed_seconds,
            delivery_retry_max_seconds=self.policy.retry_max_seconds,
            allowed_job_states=frozenset({"open"}),
            poll_seconds=self.policy.observation_seconds,
            task_settle_seconds=self.policy.settle_seconds,
            observation_recovery=ObservationRecovery(
                gap_seconds=self.policy.recovery_seconds,
                backoff_max_seconds=self.policy.retry_max_seconds,
                on_retry=self.retry_notice,
                on_recovered=self.recovery_notice,
            ),
            condition_recovery=ConditionRecovery(
                window_seconds=self.policy.recovery_seconds,
                backoff_max_seconds=self.policy.retry_max_seconds,
                on_retry=self.retry_notice,
                on_recovered=self.recovery_notice,
            ),
        )

        log.info(f"Selected {job_input.job_ref=}")

        try:
            outcome = await self.active.run(pending)
        except ControllerFailed as error:
            match await self.active.settle_revocation(error):
                case None:
                    raise

            log.info(f"Attempt revoked before a result was selected; {job_input.job_ref=}")
        else:
            match outcome:
                case DeliveryAcceptedCheckpoint():
                    log.info(f"Result accepted by shared delivery; {job_input.job_ref=}")
                case _:
                    raise RuntimeError(f"Attempt did not reach durable delivery; {job_input.job_ref=}, {type(outcome)=}")

        self.active = None

    @property
    def shutdown_ready(self):
        """Keep lifetime exclusion until Attempts, exchanges and withdrawal settle."""
        return (
            (self.active is None or self.active.shutdown_ready) and
            all(owner.shutdown_ready for owner in self.claims.live_owners()) and
            self.api.retirement_confirmed and
            self.publisher.retirement_confirmed and
            not self.publisher.withdrawal_required
        )

    async def watch_worker(self):
        """Stop offering inference when the original model process exits."""

        while not self.stopping.is_set():
            try:
                exited = await self.host.observe_exit(wait_seconds=self.policy.feed_seconds)
            except TimeoutError:
                continue

            if not self.stopping.is_set():
                raise RuntimeError(f"The scientific worker is no longer available; {exited=}")

    async def pause(self, seconds):
        """Wait for the interval or return early when shutdown is requested."""

        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        # silent-defaults: allow-exception-outcome -- The polling interval elapsed without a stop request
        except TimeoutError:
            return False

        return True

    @staticmethod
    def retry_notice(*details):
        """Log the SDK's retry evidence without selecting a new operation."""
        log.warning(f"SDK is retrying the original API or delivery operation; {details=}")

    @staticmethod
    def recovery_notice(*details):
        """Report recovery of the original owned exchange."""
        log.info(f"SDK recovered the original API operation; {details=}")
