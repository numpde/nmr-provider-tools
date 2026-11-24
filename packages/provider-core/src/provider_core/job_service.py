"""Run serial API jobs across declared offerings and their original worker hosts.

AttemptController owns START, phase observation, scientific permission and durable
result handoff. This service selects work, announces availability, and keeps those
owners alive through shutdown. Providers supply their scientific AnalysisSession.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
import secrets

from provider_core.adapters.nmr.v1.attempt_request import start_body
from provider_core.analysis import AnalysisOutcomeUnresolved, AnalysisSession
from provider_core.api_errors import ApiFailure
from provider_core.attempt_record import RecoveryScope, StartPending
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.execution import ConditionRecovery, ExecutionFailed, ObservationRecovery
from provider_core.failure_collection import FailureCollector
from provider_core.hello_publisher import HelloPublisher
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull
from provider_core.process_host import ProcessExecutionHost
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.tools import require_positive_number
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering


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


class HostFailurePolicy(StrEnum):
    """Stop the service or withdraw a host after exit or an unresolved result."""

    STOP = "stop"
    WITHDRAW = "withdraw"


@dataclass(frozen=True)
class JobOffering:
    """Map one public capability to its already-started host and job adapter.

    Several capabilities may share one host/allocation and select different
    scientific methods in their session factories. Startup and host retirement
    belong to the caller's enclosing resource scope.
    """

    announcement: AnalysisOffering
    host: ProcessExecutionHost
    consumer_id: str
    generation: str
    new_session: Callable[[], AnalysisSession]


class JobService:
    """Run one job at a time while retaining independent failed-host Attempts.

    Providers finish configuration and model startup before calling serve().
    WITHDRAW preserves healthy hosts after an unavailable worker; it never starts
    replacement workers. Shared unrecoverable API/identity failures stop the
    service. The caller keeps every host alive until service settlement finishes.
    """

    def __init__(
        self,
        *,
        provider_ref,
        offerings: tuple[JobOffering, ...],
        host_failure_policy: HostFailurePolicy,
        limits: LiveAttemptLimits,
        display_name,
        description,
        policy,
        new_api,
        new_delivery,
        stopping,
    ):
        """Retain the API clients and Attempt claims for this provider lifetime."""
        self.offerings = tuple(offerings)
        kinds = tuple(offering.announcement.analysis_kind_ref for offering in self.offerings)

        if not kinds or len(set(kinds)) != len(kinds):
            raise ValueError(f"Cannot configure the job service: offering references must be nonempty and unique; {kinds=}")

        self.host_failure_policy = HostFailurePolicy(host_failure_policy)
        self.hosts = tuple(dict.fromkeys(offering.host for offering in self.offerings))
        self.unavailable_hosts = {}
        self.cursors = dict.fromkeys(kinds)
        self.policy = policy
        self.new_api = new_api
        self.new_delivery = new_delivery
        self.api = new_api()
        self.publisher = HelloPublisher(
            new_api(), provider_ref=provider_ref, display_name=display_name,
            description=description, offerings=lambda: tuple(
                offering.announcement for offering in self.available_offerings
            ), stopping=stopping,
        )
        self.scopes = {
            offering.announcement.analysis_kind_ref: RecoveryScope(
                self.api.origin, provider_ref, offering.consumer_id, offering.generation,
            )
            for offering in self.offerings
        }
        self.stopping = stopping
        self.claims = AttemptClaims.live(limits=limits)
        self.active = None

    async def serve(self):
        """Announce loaded science, consume jobs, then settle every acquired owner."""
        failures = FailureCollector(message="Provider service and shutdown failed")

        with failures.capture():
            if not self.stopping.is_set():
                # Observe each original host before the first availability snapshot.
                # This detects an already lost worker; later exits are watched below.
                await asyncio.gather(*(self.observe_worker(host) for host in self.hosts))
                await self.publisher.refresh()

                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self.jobs())
                    tasks.create_task(self.publisher.run(
                        refresh_seconds=self.policy.hello_seconds, poll_seconds=self.policy.feed_seconds,
                    ))
                    for host in self.hosts:
                        tasks.create_task(self.watch_worker(host))

        self.stopping.set()
        controllers = tuple(dict.fromkeys((
            *self.claims.live_owners(), *((self.active,) if self.active is not None else ()),
        )))

        for controller in controllers:
            controller.request_stop()

        for controller in controllers:
            with failures.capture():
                await controller.join()

        # Retained controllers own the original reports and failed Attempts even
        # after another offering runs. Settlement never starts replacement science.
        while any(not controller.shutdown_ready for controller in controllers):
            for controller in controllers:
                if controller.shutdown_ready:
                    continue
                try:
                    await controller.settle()
                except Exception:
                    log.exception("Attempt settlement failed; retaining its original work and result")

            if any(not controller.shutdown_ready for controller in controllers):
                await asyncio.sleep(self.policy.retry_max_seconds)

        with failures.capture():
            await self.api.retire(owner=self)

        with failures.capture():
            await self.publisher.withdraw()

        with failures.capture():
            await self.publisher.retire()

        failures.raise_if_any()

    @property
    def available_offerings(self):
        """Project availability from the actual host associations."""
        return tuple(offering for offering in self.offerings if offering.host not in self.unavailable_hosts)

    async def jobs(self):
        """Poll each available offering; a shared API backoff pauses all discovery."""
        while not self.stopping.is_set():
            for offering in self.available_offerings:
                if offering.host in self.unavailable_hosts:
                    continue
                kind = offering.announcement.analysis_kind_ref
                scope = self.scopes[kind]

                try:
                    page = await self.api.jobs(
                        analysis_kind_ref=kind,
                        has_provider_execution_attempt=False,
                        limit=50,
                        cursor=self.cursors[kind],
                        owner=self,
                    )
                except ApiFailure as error:
                    await self.api.retire(owner=self)

                    if not self.api.can_retry_job_discovery(error):
                        raise

                    log.warning(f"Job discovery failed; {error=}")
                    await self.pause(max(self.policy.feed_seconds, error.retry_after_seconds or 0))
                    break

                if offering.host in self.unavailable_hosts:
                    continue
                self.cursors[kind] = page.next_cursor

                for job in page.jobs:
                    if self.stopping.is_set():
                        return
                    if offering.host in self.unavailable_hosts:
                        break
                    if self.claims.owns_job(scope, job.input.job_ref):
                        continue

                    await self.run_job(offering, job.input)

            if not any(self.cursors.values()):
                await self.pause(self.policy.feed_seconds)

    async def run_job(self, offering, job_input):
        """The SDK confirms START before invoking this session's inference call."""

        kind = offering.announcement.analysis_kind_ref
        scope = self.scopes[kind]
        pending = StartPending(
            scope,
            kind,
            job_input,
            start_body(job_input.job_ref, scope.consumer_id + ":" + secrets.token_hex(32)),
        )

        self.active = AttemptController.live(
            self.new_api(),
            self.claims,
            offering.new_session(),
            scope=scope,
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
            if (self.host_failure_policy is HostFailurePolicy.WITHDRAW and
                    type(error.cause) is ExecutionFailed and
                    type(error.cause.cause) is AnalysisOutcomeUnresolved):
                self.active.require_held_failure(pending, error)
                self.withdraw_host(offering.host, error)
            elif type(error.cause) is LiveCapacityFull:
                await self.active.settle()
                await self.pause(self.policy.feed_seconds)
            else:
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

    def withdraw_host(self, host, cause):
        """Withdraw all dependent offerings without releasing their Attempt owners."""
        self.unavailable_hosts.setdefault(host, cause)

        for offering in self.offerings:
            if offering.host is host:
                self.cursors[offering.announcement.analysis_kind_ref] = None

        if (self.host_failure_policy is HostFailurePolicy.STOP or
                len(self.unavailable_hosts) == len(self.hosts)):
            raise RuntimeError(f"Cannot continue the job service: {len(self.unavailable_hosts)=} of {len(self.hosts)=} scientific hosts are unavailable under {self.host_failure_policy=}") from (
                cause if isinstance(cause, BaseException) else None
            )

    async def observe_worker(self, host):
        """Retain original worker loss, including uncertain monitor failures."""
        try:
            exited = await host.observe_exit(wait_seconds=self.policy.feed_seconds)
        except TimeoutError:
            return
        except Exception as cause:
            exited = cause

        if not self.stopping.is_set():
            self.withdraw_host(host, exited)

    async def watch_worker(self, host):
        """One passive observer covers every offering on the same native host."""
        while not self.stopping.is_set() and host not in self.unavailable_hosts:
            await self.observe_worker(host)

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
