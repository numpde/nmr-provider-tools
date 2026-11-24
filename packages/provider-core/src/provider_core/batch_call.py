"""Collect bounded per-job inputs for one exclusively owned scientific call.

The provider owns pack/unpack and scientific schemas. This dispatcher owns the
ordered participants, shared invocation and settlement. It provides no replay or
partial-result recovery: the complete reply must validate before any entry moves
back to its Attempt. Keep the process-call context outside this owner's scope.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass
from functools import partial

from provider_core.failure_collection import FailureCollector
from provider_core.hosting import _join_settlement, _raise_context_failure, _start_owned_task
from provider_core.process_call import ProcessCall
from provider_core.scientific_invocation import ScientificInvocation, open_invocation
from provider_core.tools import require_positive_number
from provider_core.type_requirements import require_instance, require_type


@dataclass(frozen=True, kw_only=True)
class BatchPolicy:
    maximum_entries: int
    collection_seconds: float
    maximum_queued_input_bytes: int
    maximum_entry_input_bytes: int

    def __post_init__(self):
        for name in ("maximum_entries", "maximum_queued_input_bytes", "maximum_entry_input_bytes"):
            limit_value = getattr(self, name)

            require_type(limit_value, int)

            if not (limit_value >= 1):
                raise ValueError(f"Cannot configure batching: {name=} must be positive; {limit_value=}")

        require_positive_number(self.collection_seconds)

        if not (self.maximum_entry_input_bytes <= self.maximum_queued_input_bytes):
            raise ValueError(f"Cannot queue a maximum-sized entry: {self.maximum_entry_input_bytes=} exceeds {self.maximum_queued_input_bytes=}")


class _MembershipChanged(Exception):
    """The permission callback refused before ScientificCall acquired an analysis."""


class BatchParticipation:
    """One Attempt's unique slot, retained until shared scientific work settles."""

    def __init__(self, owner, payload):
        self.owner = owner
        self.payload = payload
        self.require_execution = None
        self.ready_at = None
        self.dispatched = False
        self.retired = False
        self.settled = asyncio.get_running_loop().create_future()
        self.entry_result = None
        self.error = None
        self.retirement_error = None

    async def execute(self, *, require_execution):
        """Activate once under running-phase permission and await this entry only."""
        require_execution()

        if not ((self.ready_at is None) and (not self.retired)):
            raise RuntimeError(f"Cannot activate this batch participation twice or after retirement: {self.ready_at=}, {self.retired=}")

        self.require_execution = require_execution
        self.ready_at = asyncio.get_running_loop().time()

        self.owner.changed.set()
        await asyncio.shield(self.settled)

        if not (self.error is None):
            raise self.error

        return self.entry_result

    async def retire(self):
        """Remove an undispatched entry, or join its shared work without stopping siblings."""
        self.retired = True

        if not self.settled.done():
            if not self.dispatched:
                self.owner.finish(self, error=asyncio.CancelledError())
            else:
                self.owner.stop_if_unneeded()

        await asyncio.shield(self.settled)

        if not (self.retirement_error is None):
            raise self.retirement_error


class BatchCall:
    """Batch only participants targeting this one call/model/settings identity.

    prepare() retains encoded snapshots; queued and dispatched bytes share one
    budget. Call it under JobService's preparation limit: a preparation waiting
    for space holds at most maximum_entry_input_bytes outside the queue. Attempt
    claims separately bound raw inputs and result/delivery retention. The process
    call's encoded-input/result limits bound the assembled wire documents.

    ProcessCall grants retirement authority over its privately owned worker.
    Borrowed ScientificCalls cannot grant that authority and are rejected.
    Retries are disabled because encoded membership cannot survive revocation.
    """

    def __init__(self, *, call: ProcessCall, policy, entry_codec, pack, unpack):
        require_instance(
            call,
            ProcessCall,
            error_factory=lambda diagnostic: TypeError("Batching requires a ProcessCall that owns its worker; " + diagnostic),
        )

        if not (call.error_policy.max_attempts == 1):
            raise ValueError(f"Cannot share a batch with {call.error_policy.max_attempts=}: configure one attempt so revoked participants cannot be replayed")

        self.call = call
        self.policy = policy
        self.entry_codec = entry_codec
        self.pack = pack
        self.unpack = unpack
        self.participants = []
        self.input_bytes = 0
        self.changed = asyncio.Event()
        self.closed = False
        self.runner = None
        self.invocation = None
        self.dispatched = ()
        self.retirement = None
        self.retirement_settled = False

    async def __aenter__(self):
        if not ((self.runner is None) and (not self.closed)):
            raise RuntimeError(f"Cannot reopen a batch dispatcher: {self.runner is not None=}, {self.closed=}")

        self.call._claim_batch(self)

        try:
            self.runner = ScientificInvocation(_start_owned_task(operation=self.dispatch_batches))
        except BaseException:
            self.call._release_batch(self)
            raise

        return self

    async def __aexit__(self, exception_type, exception, traceback):
        try:
            await self.close()
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    async def prepare(self, scientific_input):
        """Freeze one input and wait for bounded queue space before returning its owner."""
        payload = self.entry_codec.encode(scientific_input)

        if not (len(payload) <= self.policy.maximum_entry_input_bytes):
            raise ValueError(f"Cannot queue scientific input: {len(payload)=} exceeds {self.policy.maximum_entry_input_bytes=}")

        while True:
            if not ((not self.closed) and (self.runner is not None)):
                raise RuntimeError(f"Cannot queue scientific input: the batch dispatcher is not open; {self.runner is not None=}, {self.closed=}")

            if (self.input_bytes + len(payload)) <= self.policy.maximum_queued_input_bytes:
                entry = BatchParticipation(self, payload)

                self.participants.append(entry)
                self.input_bytes += len(payload)

                return entry

            self.changed.clear()
            await self.changed.wait()

    def finish(self, entry, *, entry_result=None, error=None, retirement_error=None):
        if entry.settled.done():
            return

        if retirement_error is None:
            self.input_bytes -= len(entry.payload)
            entry.payload = b""

            self.participants.remove(entry)

        entry.entry_result = entry_result
        entry.error = error
        entry.retirement_error = retirement_error

        entry.settled.set_result(None)
        self.changed.set()

    def stop_if_unneeded(self):
        if (self.invocation is not None) and all(entry.retired for entry in self.dispatched):
            self.invocation.request_stop()

    async def collect(self):
        while True:
            self.changed.clear()
            ready = tuple(sorted((entry for entry in self.participants if entry.ready_at is not None), key=lambda entry: entry.ready_at))

            if ready:
                remaining = ready[0].ready_at + self.policy.collection_seconds - asyncio.get_running_loop().time()

                if (len(ready) >= self.policy.maximum_entries) or (remaining <= 0):
                    return ready[:self.policy.maximum_entries]

                try:
                    async with asyncio.timeout(remaining):
                        await self.changed.wait()
                # The collection deadline triggers dispatch of the ready snapshot.
                except TimeoutError:
                    return ready[:self.policy.maximum_entries]
            else:
                await self.changed.wait()

    def require_permissions(self, participants):
        changed = False

        for entry in participants:
            if entry.settled.done():
                changed = True

                continue

            try:
                entry.require_execution()
            except Exception as error:
                self.finish(entry, error=error)
                changed = True

        if changed:
            raise _MembershipChanged()

        # No await separates these checks from ScientificCall's submission.
        self.dispatched = participants

        for entry in participants:
            entry.dispatched = True

    async def dispatch(self, participants):
        # repo-check: allow-tuple-generator -- Retain the exact ordered participants across the shared invocation and its cleanup.
        participants = tuple(entry for entry in participants if not entry.settled.done())

        while participants:
            # repo-check: allow-tuple-generator -- Provider packing receives one immutable ordered snapshot matching the retained participants.
            scientific_input = self.pack(tuple(self.entry_codec.decode(entry.payload) for entry in participants))

            try:
                async with open_invocation(
                    partial(
                        self.call,
                        scientific_input=scientific_input,
                        require_execution=partial(self.require_permissions, participants),
                        _batch_owner=self,
                    ),
                ) as invocation:
                    self.invocation = invocation

                    try:
                        batch_reply = await invocation.wait()
                    except asyncio.CancelledError:
                        if not asyncio.current_task().cancelling() and all(entry.retired for entry in participants):
                            for entry in participants:
                                self.finish(entry, error=asyncio.CancelledError())

                            return

                        raise
            except _MembershipChanged:
                # Confirmed refusal at the pre-effect permission callback; no
                # analysis was acquired. Repack only the surviving participants.
                # repo-check: allow-tuple-generator -- A pre-effect refusal replaces the snapshot with exactly its surviving participants.
                participants = tuple(entry for entry in participants if not entry.settled.done())

                continue
            finally:
                self.invocation = None
                self.dispatched = ()

            entry_results = tuple(self.unpack(batch_reply))

            if not (len(entry_results) == len(participants)):
                raise ValueError(f"Cannot route the batch reply: {len(entry_results)=} differs from {len(participants)=}; no entry was delivered")

            for (entry, entry_result) in zip(participants, entry_results, strict=True):
                self.finish(entry, entry_result=entry_result)

            return

    async def retire_failed_host(self):
        """Retire shared work once; an observed failure alone does not prove settlement."""
        self.retirement = self.call.host.retire()

        observation = await self.retirement.observe(after=None, wait_seconds=0)

        while (not observation.settled) and (not observation.failures):
            observation = await self.retirement.observe(after=observation.revision, wait_seconds=1)

        self.retirement_settled = observation.settled

        failures = FailureCollector(message="Shared batch host retirement failed")

        for failure in observation.failures:
            with failures.capture():
                raise failure.cause

        failures.raise_if_any()

    async def dispatch_batches(self):
        try:
            while True:
                participants = await self.collect()

                await self.dispatch(participants)
        except BaseException as error:
            self.closed = True

            if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                for entry in tuple(self.participants):
                    self.finish(entry, error=error)

                self.changed.set()
                raise

            failures = FailureCollector(message="Batch execution and shared host retirement failed")
            retirement_error = None

            with failures.capture():
                raise error

            with failures.capture():
                try:
                    await _join_settlement(task=_start_owned_task(operation=self.retire_failed_host))
                except BaseException as cleanup:
                    retirement_error = cleanup

                    raise

            # Only the dispatcher retires a failed host. The original outcome
            # stays unresolved even when retirement is confirmed. If retirement
            # is unconfirmed, keep each entry's bytes and prevent Attempt release.
            for entry in tuple(self.participants):
                self.finish(
                    entry,
                    error=error,
                    # Confirmed settlement permits retirement; the runner preserves the failures.
                    retirement_error=None if self.retirement_settled else retirement_error,
                )

            self.changed.set()
            failures.raise_if_any()

    async def close(self):
        """Stop acquisition and join the collector and original shared invocation."""
        self.closed = True

        self.changed.set()

        try:
            if self.runner is not None:
                await self.runner.close()
        except BaseException as error:
            for entry in tuple(self.participants):
                self.finish(entry, error=error, retirement_error=error)

            raise
        else:
            # Cancellation before the collector's first turn never enters dispatch_batches().
            for entry in tuple(self.participants):
                self.finish(entry, error=asyncio.CancelledError())

            if self.runner is not None:
                self.call._release_batch(self)
