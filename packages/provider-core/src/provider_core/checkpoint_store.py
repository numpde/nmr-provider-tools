"""Own compact producer checkpoints, never scientific results or history.

SQLite file admission and durability follow delivery_store at 71dd6a8. The
single-original-operation gate is ported from attempt_store at 5229b60; it stays
here while its file-backed predecessor is removed from production consumers.
Cancellation stops a wait, not a transaction. Only settle observes that same
operation before another command, including close, can enter.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import json
import math
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from provider_core.attempt_record import RecoveryScope, StartPending, TerminalOperation
from provider_core.attempt_response import AttemptObservation
from provider_core.delivery_store import DeliveryOutcome
from provider_core.lifetime import LifetimeRefused, ProviderLifetime
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint,
    MAX_CHECKPOINT_BYTES, SuppressedCheckpoint, TerminalSelectedCheckpoint,
    checkpoint_acknowledgement, checkpoint_start, decode_checkpoint, encode_checkpoint,
)


class CheckpointRefused(RuntimeError):
    """No transition ran: its current state, scope or capacity does not permit it."""


class CheckpointFailed(RuntimeError):
    """Storage failed or its outcome remains uncertain; preserve original work."""

    # An expired wait has no observed disk failure yet; later settlement retains the original operation.
    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class CheckpointLimits:
    maximum_records: int
    maximum_bytes: int

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in (self.maximum_records, self.maximum_bytes)):
            raise ValueError("Producer checkpoint item and metadata byte limits must be positive integers.")


def _key(checkpoint):
    return json.loads(checkpoint_start(checkpoint).body)["provider_attempt_key"]


def _acceptance(selection):
    acknowledged = checkpoint_acknowledgement(selection)
    start = acknowledged.start
    return {
        "api_origin": start.scope.api_origin, "provider_ref": start.scope.provider_ref,
        "job_ref": start.input.job_ref, "execution_attempt_ref": acknowledged.admission.execution_attempt_ref,
        "operation": selection.operation.value, "body_sha256": selection.body_sha256,
    }


class _CheckpointDatabase:
    """One executor thread owns the connection and every conditional transition."""

    def __init__(self, root, scope, limits):
        self.root, self.scope, self.limits = root, scope, limits
        self._db = self._failure = None
        self._lifetime = ProviderLifetime(root)

    def _private_file(self, name):
        descriptor = os.open(self.root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            facts = os.fstat(descriptor)
            if (not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.geteuid()
                    or stat.S_IMODE(facts.st_mode) != 0o600):
                raise CheckpointRefused(f"Cannot open producer checkpoint file {name}: require a caller-owned mode 0600 regular file.")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def open(self):
        if self._db is not None:
            raise CheckpointRefused("Producer checkpoint storage is already open; retain its original owner.")
        try:
            self._lifetime.open()
        except LifetimeRefused as cause:
            raise CheckpointRefused(str(cause)) from None
        try:
            descriptor = self._private_file("checkpoints.sqlite3")
            os.close(descriptor)
            self._db = sqlite3.connect(self.root / "checkpoints.sqlite3", timeout=1)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=EXTRA")
            self._db.execute("PRAGMA secure_delete=ON")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone() is not None:
                    raise CheckpointRefused("Cannot adopt another application's database as producer checkpoints.")
                with self._transaction():
                    self._db.execute("CREATE TABLE checkpoints (start_key TEXT PRIMARY KEY NOT NULL, checkpoint BLOB NOT NULL)")
                    self._db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise CheckpointRefused("Producer checkpoint database version is unsupported.")
            self.inventory()
        except BaseException:
            # A failed close remains visible as exception context. Never report
            # successful retirement while an acquired handle is still retained.
            self.close()
            raise

    @contextmanager
    def _access(self):
        if self._failure is not None:
            raise self._failure
        if self._db is None:
            raise CheckpointRefused("Producer checkpoint database is not open.")
        try:
            yield
        except sqlite3.Error as cause:
            self._failure = CheckpointFailed("Producer checkpoint storage failed; preserve the database and reconcile the original operation before continuing.", cause=cause)
            raise self._failure from None

    @contextmanager
    def _transaction(self):
        with self._access(), self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def _encode(self, checkpoint):
        if checkpoint_start(checkpoint).scope != self.scope:
            raise CheckpointRefused("Producer checkpoint belongs to another API, provider, lane or generation.")
        return encode_checkpoint(checkpoint)

    def _require(self, expected):
        raw = self._encode(expected)
        row = self._db.execute("SELECT checkpoint FROM checkpoints WHERE start_key=?", (_key(expected),)).fetchone()
        if row != (raw,):
            raise CheckpointRefused("Producer checkpoint is absent or has advanced; reconcile its exact retained state before another effect.")
        return expected

    def _require_capacity(self, *, replacing, new_bytes):
        count, size = self._db.execute("SELECT COUNT(*),COALESCE(SUM(length(checkpoint)),0) FROM checkpoints").fetchone()
        # silent-defaults: allow-contract -- A new insertion replaces zero bytes; an existing row contributes its measured byte count.
        replacing_bytes = 0 if replacing is None else replacing
        if count + (replacing is None) > self.limits.maximum_records or size - replacing_bytes + new_bytes > self.limits.maximum_bytes:
            raise CheckpointRefused("Producer checkpoint capacity is full; settle existing obligations before admitting more work.")

    def inventory(self):
        with self._access():
            count, largest, size = self._db.execute("SELECT COUNT(*),COALESCE(MAX(length(checkpoint)),0),COALESCE(SUM(length(checkpoint)),0) FROM checkpoints").fetchone()
            if count > self.limits.maximum_records or largest > MAX_CHECKPOINT_BYTES or size > self.limits.maximum_bytes:
                raise CheckpointRefused("Retained producer checkpoints exceed their configured item or metadata byte bound.")
            result = []
            for key, raw in self._db.execute("SELECT start_key,checkpoint FROM checkpoints ORDER BY start_key"):
                checkpoint = decode_checkpoint(raw, scope=self.scope)
                if _key(checkpoint) != key:
                    raise CheckpointRefused("Retained producer checkpoint does not match its original START key.")
                result.append(checkpoint)
            return tuple(result)

    def retain_start(self, pending):
        if type(pending) is not StartPending:
            raise CheckpointRefused("Only an exact pending START may be newly retained.")
        raw = self._encode(pending)
        with self._transaction():
            row = self._db.execute("SELECT checkpoint FROM checkpoints WHERE start_key=?", (_key(pending),)).fetchone()
            if row is not None:
                return self._require(pending)
            self._require_capacity(replacing=None, new_bytes=len(raw))
            self._db.execute("INSERT INTO checkpoints VALUES (?,?)", (_key(pending), raw))
        return pending

    def require_start(self, pending):
        if type(pending) is not StartPending:
            raise CheckpointRefused("Resume requires the exact retained pending START.")
        with self._access():
            return self._require(pending)

    def _advance(self, expected, replacement):
        raw = self._encode(replacement)
        with self._transaction():
            self._require(expected)
            self._require_capacity(replacing=len(self._encode(expected)), new_bytes=len(raw))
            self._db.execute("UPDATE checkpoints SET checkpoint=? WHERE start_key=?", (raw, _key(expected)))
        return replacement

    def acknowledge_start(self, pending, admission):
        if type(pending) is not StartPending:
            raise CheckpointRefused("START acknowledgement requires its exact pending request.")
        return self._advance(pending, AcknowledgedCheckpoint(pending, admission))

    def mark_entered(self, acknowledged, prepared_digest):
        if type(acknowledged) is not AcknowledgedCheckpoint:
            raise CheckpointRefused("Scientific entry requires the exact acknowledged checkpoint.")
        return self._advance(acknowledged, EnteredCheckpoint(acknowledged, prepared_digest))

    def select_terminal(self, attempt, outcome):
        if type(outcome) is not DeliveryOutcome:
            raise CheckpointRefused("Select the actual approved delivery outcome before enqueueing it.")
        if outcome.operation is TerminalOperation.COMPLETE and type(attempt) is not EnteredCheckpoint:
            raise CheckpointRefused("A successful scientific outcome requires its retained execution entry.")
        selected = TerminalSelectedCheckpoint(attempt, outcome.operation, outcome.body_sha256, len(outcome.body))
        if outcome.acceptance() != _acceptance(selected):
            raise CheckpointRefused("Selected terminal request does not belong to this acknowledged Job and Attempt.")
        return self._advance(attempt, selected)

    def accept_delivery(self, selected, acceptance):
        if type(selected) is not TerminalSelectedCheckpoint or acceptance != _acceptance(selected):
            raise CheckpointRefused("Delivery acknowledgement must match the exact selected terminal command.")
        return self._advance(selected, DeliveryAcceptedCheckpoint(selected))

    def suppress(self, attempt, reason):
        return self._advance(attempt, SuppressedCheckpoint(attempt, reason))

    def clear_after_cleanup(self, accepted):
        if type(accepted) is not DeliveryAcceptedCheckpoint:
            raise CheckpointRefused("Clearing a handed-off checkpoint requires its accepted delivery identity and caller-confirmed cleanup.")
        self._clear(accepted)

    def clear_terminal_start_after_cleanup(self, acknowledged):
        if type(acknowledged) is not AcknowledgedCheckpoint or acknowledged.admission.state == "in_progress":
            raise CheckpointRefused("Clearing a terminal START replay requires its actual terminal admission and caller-confirmed cleanup.")
        self._clear(acknowledged)

    def clear_observed_after_cleanup(self, checkpoint, observation):
        acknowledged = checkpoint_acknowledgement(checkpoint)
        if (type(observation) is not AttemptObservation or observation.state == "in_progress"
                or observation.execution_attempt_ref != acknowledged.admission.execution_attempt_ref
                or observation.job_ref != acknowledged.start.input.job_ref):
            raise CheckpointRefused("Clearing interrupted control requires its matching terminal API observation and caller-confirmed cleanup.")
        self._clear(checkpoint)

    def _clear(self, checkpoint):
        with self._transaction():
            self._require(checkpoint)
            self._db.execute("DELETE FROM checkpoints WHERE start_key=?", (_key(checkpoint),))

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        self._lifetime.close()


@dataclass(slots=True)
class _Pending:
    operation: str
    worker: object
    waiter: asyncio.Future
    closing: bool
    owner: object


@dataclass(frozen=True, slots=True)
class SettledCheckpoint:
    operation: str
    value: object = field(repr=False)


def _observe_wrapper_failure(waiter):
    if not waiter.cancelled():
        waiter.exception()


class CheckpointStore:
    """Bounded compact decisions with one original outstanding disk operation."""

    def __init__(self, root, *, scope, limits, operation_seconds):
        if not isinstance(root, Path) or not root.is_absolute() or type(scope) is not RecoveryScope or type(limits) is not CheckpointLimits:
            raise ValueError("Producer checkpoints require an absolute private root, explicit recovery scope and metadata limits.")
        if not (type(operation_seconds) in (int, float) and math.isfinite(operation_seconds) and 0 < operation_seconds):
            raise ValueError("Producer checkpoint wait limit must be finite and positive.")
        self._database = _CheckpointDatabase(root, scope, limits)
        self._seconds = operation_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-checkpoints")
        self._pending = self._loop = None
        self._closed = self._close_failed = self._observing = False
        self.last_failure = None

    @property
    def scope(self):
        return self._database.scope

    @property
    def pending_operation(self):
        # silent-defaults: allow-contract -- No outstanding disk operation means no pending operation name.
        return None if self._pending is None else self._pending.operation

    @property
    def pending_owner(self):
        # silent-defaults: allow-contract -- No outstanding disk operation means no retained disk owner.
        return None if self._pending is None else self._pending.owner

    @property
    def retirement_confirmed(self):
        return self._closed and not self._close_failed and self._pending is None

    async def open(self):
        return await self._run("open producer checkpoints", self._database.open)

    async def inventory(self, *, owner=None):
        return await self._run("read producer checkpoints", self._database.inventory, owner=owner)

    async def retain_start(self, pending, *, owner=None):
        return await self._run("retain pending START checkpoint", self._database.retain_start, pending, owner=owner)

    async def require_start(self, pending, *, owner=None):
        return await self._run("require pending START checkpoint", self._database.require_start, pending, owner=owner)

    async def acknowledge_start(self, pending, admission, *, owner=None):
        return await self._run("retain START acknowledgement checkpoint", self._database.acknowledge_start, pending, admission, owner=owner)

    async def mark_entered(self, acknowledged, prepared_digest, *, owner=None):
        return await self._run("retain scientific entry digest", self._database.mark_entered, acknowledged, prepared_digest, owner=owner)

    async def select_terminal(self, attempt, outcome, *, owner=None):
        return await self._run("retain selected terminal identity", self._database.select_terminal, attempt, outcome, owner=owner)

    async def accept_delivery(self, selected, acceptance, *, owner=None):
        return await self._run("retain accepted delivery identity", self._database.accept_delivery, selected, acceptance, owner=owner)

    async def suppress(self, attempt, reason, *, owner=None):
        return await self._run("retain suppressed disposition", self._database.suppress, attempt, reason, owner=owner)

    async def clear_after_cleanup(self, accepted, *, owner=None):
        return await self._run("clear retired producer checkpoint", self._database.clear_after_cleanup, accepted, owner=owner)

    async def clear_terminal_start_after_cleanup(self, acknowledged, *, owner=None):
        return await self._run("clear terminal START checkpoint", self._database.clear_terminal_start_after_cleanup, acknowledged, owner=owner)

    async def clear_observed_after_cleanup(self, checkpoint, observation, *, owner=None):
        return await self._run("clear observed terminal checkpoint", self._database.clear_observed_after_cleanup, checkpoint, observation, owner=owner)

    async def settle(self, *, owner=None):
        if self._pending is None or self._pending.owner is not owner:
            raise CheckpointRefused("Only the original checkpoint operation owner can settle an unresolved disk result.")
        operation = self._pending.operation
        return SettledCheckpoint(operation, await self._observe())

    async def close(self):
        if self._closed:
            if self._close_failed:
                raise CheckpointRefused("Producer checkpoint retirement failed; enclosing process retirement is required.")
            return
        if self._pending is not None:
            raise CheckpointRefused("Settle the original checkpoint operation before closing its database.")
        await self._run("close producer checkpoints", self._database.close, closing=True)

    # silent-defaults: allow-contract -- Ordinary disk commands preserve the store; the explicit close command supplies closing=True.
    async def _run(self, operation, function, *args, closing=False, owner=None):
        if self._closed or self._pending is not None:
            raise CheckpointRefused("Checkpoint store is closed or owns an unobserved disk result; no new operation was submitted.")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise CheckpointRefused("Checkpoint operations must remain on their owning event loop.")
        self._loop = loop
        worker = self._executor.submit(function, *args)
        waiter = asyncio.wrap_future(worker, loop=loop)
        waiter.add_done_callback(_observe_wrapper_failure)
        self._pending = _Pending(operation, worker, waiter, closing, owner)
        self.last_failure = None
        return await self._observe()

    async def _observe(self):
        if self._observing or asyncio.get_running_loop() is not self._loop:
            raise CheckpointRefused("Only one task on the owning event loop may observe checkpoint I/O.")
        self._observing = True
        pending = self._pending
        try:
            done, _ = await asyncio.wait({pending.waiter}, timeout=self._seconds)
            if not done:
                self.last_failure = CheckpointFailed(f"Waiting to {pending.operation} exceeded its limit; settle the same original disk operation before reuse.")
                raise self.last_failure
            self._pending = None
            try:
                return pending.worker.result()
            except CheckpointRefused:
                raise
            except BaseException as cause:
                if pending.closing:
                    self._close_failed = True
                self.last_failure = cause if isinstance(cause, CheckpointFailed) else CheckpointFailed(f"Cannot {pending.operation}; inspect the protected storage cause.", cause=cause)
                raise self.last_failure from None
            finally:
                if pending.closing:
                    self._executor.shutdown(wait=False)
                    self._closed = True
        except asyncio.CancelledError as cause:
            self.last_failure = CheckpointFailed(f"Waiting to {pending.operation} was cancelled; the original disk operation remains owned.", cause=cause)
            raise
        finally:
            self._observing = False
