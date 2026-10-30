"""Keep only outstanding NMR API publication requests in private SQLite storage.

Ports Magnet 0619d9ffd92a4326758b442c532aa09496161098,
magnet_provider/terminal_outbox.py:71-126,315-359. Unlike that outbox, matching
publication removes the payload; there is no completed-result archive. This
owner serializes synchronous transactions so the daemon can offload disk I/O.
"""

from provider_core.container_guard import require_container

require_container()

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import read_complete_body, read_fail_body, require_provider_ref
from provider_core.attempt_response import require_attempt_ref
from provider_core.signing import _https_origin
from provider_core.upload_response import require_job_ref


# Bound the shared daemon's supported fanout before granting per-binding state.
# repo-check: allow-module-constant
MAX_BINDINGS = 16

# The maximum escaped inventory row is 6604 bytes. This occupancy leaves over
# 396 KiB of the 2 MiB IPC response envelope for binding identity and status.
# repo-check: allow-module-constant
MAX_PROVIDER_ITEMS = 256


class DeliveryConflict(RuntimeError):
    """An outstanding immutable request cannot be replaced by this submission."""


class DeliveryFull(RuntimeError):
    """No request was accepted; keep the producer's outcome and apply backpressure."""


class DeliveryInventoryUnavailable(RuntimeError):
    """An oversized legacy inventory cannot be sent as one bounded IPC reply."""

    def __init__(self):
        super().__init__(
            "Pending inventory exceeds 256 items; publication continues and all rows remain retained. "
            "Read known Attempt status individually or retry inventory after the publisher drains the queue."
        )


class DeliveryStorageFailed(RuntimeError):
    """Safe storage context with the original cause for protected diagnostics."""

    # Private resource admission can refuse before an underlying OS or SQLite operation supplies a cause.
    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    api_origin: str
    provider_ref: str
    job_ref: str
    execution_attempt_ref: str
    operation: TerminalOperation
    body: bytes = field(repr=False)

    def __post_init__(self):
        if _https_origin(self.api_origin)[0] != self.api_origin:
            raise ValueError("Delivery requires the configured canonical API origin.")
        require_provider_ref(self.provider_ref)
        require_job_ref(self.job_ref)
        require_attempt_ref(self.execution_attempt_ref)
        if type(self.operation) is not TerminalOperation:
            raise ValueError("Delivery requires an explicit complete or fail operation.")
        parser = read_complete_body if self.operation is TerminalOperation.COMPLETE else read_fail_body
        parser(self.body, execution_attempt_ref=self.execution_attempt_ref)

    @property
    def key(self):
        return self.api_origin, self.provider_ref, self.execution_attempt_ref

    @property
    def body_sha256(self):
        return "sha256:" + hashlib.sha256(self.body).hexdigest()

    def acceptance(self):
        """Durable enqueue identity; this is not an API acceptance receipt."""
        return {
            "api_origin": self.api_origin, "provider_ref": self.provider_ref,
            "job_ref": self.job_ref, "execution_attempt_ref": self.execution_attempt_ref,
            "operation": self.operation.value, "body_sha256": self.body_sha256,
        }


@dataclass(frozen=True, slots=True)
class QueueLimits:
    provider_items: int
    provider_bytes: int
    total_items: int
    total_bytes: int

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (
            self.provider_items, self.provider_bytes, self.total_items, self.total_bytes
        )):
            raise ValueError("Delivery queue item and byte limits must be positive integers.")
        if not self.provider_items <= MAX_PROVIDER_ITEMS:
            raise ValueError("Delivery provider_items cannot exceed 256: the complete pending inventory must fit the supported IPC response envelope.")
        if not (self.provider_items <= self.total_items and self.provider_bytes <= self.total_bytes):
            raise ValueError("A provider's delivery allowance cannot exceed the host allowance.")


class DeliveryQueue:
    """The sole connection owns pending data; producers never open this database."""

    def __init__(self, directory, *, limits):
        if type(limits) is not QueueLimits:
            raise ValueError("Delivery storage requires explicit queue limits.")
        self.limits, self.directory = limits, directory
        self._serial = threading.RLock()
        self._db, self._lock, self._failure = None, None, None
        try:
            facts = directory.lstat()
            if (not directory.is_absolute() or directory.resolve() != directory
                    or not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.geteuid()
                    or stat.S_IMODE(facts.st_mode) != 0o700):
                raise DeliveryStorageFailed("Cannot open delivery storage: its directory must be canonical, caller-owned and mode 0700.")
            self._lock = self._private_file(directory / ".owner.lock")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor = self._private_file(directory / "delivery.sqlite3")
            os.close(descriptor)
            self._db = sqlite3.connect(directory / "delivery.sqlite3", check_same_thread=False, timeout=1)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=EXTRA")
            self._db.execute("PRAGMA secure_delete=ON")
            self._initialize()
        except (OSError, sqlite3.Error, DeliveryStorageFailed) as cause:
            self.close()
            if type(cause) is DeliveryStorageFailed:
                raise
            raise DeliveryStorageFailed("Cannot open delivery storage; restore its private database and single-writer ownership.", cause=cause) from None

    def _private_file(self, path):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                facts = os.fstat(descriptor)
            except OSError as cause:
                raise DeliveryStorageFailed(f"Cannot inspect private delivery file {path.name} before acquiring it.", cause=cause) from None
            if (not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.geteuid()
                    or stat.S_IMODE(facts.st_mode) != 0o600):
                raise DeliveryStorageFailed(
                    f"Cannot open private delivery file {path.name}: require a caller-owned mode 0600 regular file; "
                    f"observed uid {facts.st_uid}, mode {stat.S_IMODE(facts.st_mode):04o}, caller uid {os.geteuid()}."
                )
        except BaseException:
            # Until return, this descriptor belongs here rather than self._lock
            # or SQLite. A close failure retains the admission failure as context.
            os.close(descriptor)
            raise
        return descriptor

    def _initialize(self):
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise DeliveryStorageFailed("Cannot open delivery storage: its database version is unsupported.")
        if version == 0:
            if self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone() is not None:
                raise DeliveryStorageFailed("Cannot adopt another application's database as the delivery queue.")
            with self._transaction("initialize"):
                self._db.execute("""CREATE TABLE outstanding (
                    api_origin TEXT NOT NULL, provider_ref TEXT NOT NULL, attempt_ref TEXT NOT NULL,
                    job_ref TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('complete','fail')),
                    body BLOB NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','held')),
                    created_at REAL NOT NULL, held_reason TEXT,
                    PRIMARY KEY(api_origin, provider_ref, attempt_ref))""")
                self._db.execute("PRAGMA user_version=1")
            version = 1
        if version == 1:
            with self._transaction("upgrade"):
                self._db.execute("ALTER TABLE outstanding ADD COLUMN publication BLOB")
                self._db.execute("PRAGMA user_version=2")
        # Detect incompatible/corrupt schema before serving any publication.
        self._db.execute("SELECT api_origin,provider_ref,attempt_ref,job_ref,operation,body,digest,state,created_at,held_reason,publication FROM outstanding LIMIT 0")

    @contextmanager
    def _access(self, operation):
        with self._serial:
            if self._failure is not None:
                raise self._failure
            if self._db is None:
                raise DeliveryStorageFailed("Cannot use delivery storage after it closed.")
            try:
                yield
            except sqlite3.Error as cause:
                self._failure = DeliveryStorageFailed(
                    f"Cannot {operation} delivery storage. Preserve the database and restore storage before continuing; earlier delivery acknowledgements and API receipts are unchanged.",
                    cause=cause,
                )
                raise self._failure from None

    @contextmanager
    def _transaction(self, operation):
        with self._access(operation), self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def enqueue(self, item):
        return self._enqueue(item, None, item.acceptance())

    def enqueue_handoff(self, handoff):
        from provider_core.delivery_handoff import DeliveryHandoff
        if type(handoff) is not DeliveryHandoff:
            raise ValueError("Delivery requires a complete publication handoff.")
        return self._enqueue(handoff.outcome, handoff.publication_bytes(), handoff.acceptance())

    def _enqueue(self, item, publication, acceptance):
        with self._transaction("enqueue into"):
            existing = self._db.execute(
                "SELECT job_ref,operation,body,publication FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=?", item.key
            ).fetchone()
            if existing is not None:
                if existing != (item.job_ref, item.operation.value, item.body, publication):
                    raise DeliveryConflict("This Attempt already has a different outstanding terminal request; the original remains unchanged.")
                return acceptance
            for where, parameters, maximum_items, maximum_bytes in (
                (" WHERE api_origin=? AND provider_ref=?", item.key[:2], self.limits.provider_items, self.limits.provider_bytes),
                ("", (), self.limits.total_items, self.limits.total_bytes),
            ):
                count, size = self._db.execute("SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM outstanding" + where, parameters).fetchone()
                if count >= maximum_items or size + len(item.body) > maximum_bytes:
                    raise DeliveryFull("Delivery capacity is full. Keep this outcome, wait for space and stop admitting more computation.")
            self._db.execute("INSERT INTO outstanding (api_origin,provider_ref,attempt_ref,job_ref,operation,body,digest,state,created_at,held_reason,publication) VALUES (?,?,?,?,?,?,?,'pending',?,NULL,?)",
                             (*item.key, item.job_ref, item.operation.value, item.body, item.body_sha256, time.time(), publication))
        return acceptance

    def next_pending_handoff(self, api_origin, provider_ref):
        """Return the oldest publishable row with its own original signing material."""
        from provider_core.delivery_handoff import DeliveryHandoffRejected, read_delivery_handoff
        with self._access("read a publication from"):
            item = self.next_pending(api_origin, provider_ref)
            if item is None:
                return None
            publication = self._db.execute(
                "SELECT publication FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=?", item.key
            ).fetchone()[0]
            if publication is None:
                raise DeliveryStorageFailed("Cannot publish a legacy delivery without its original publication material; preserve and hold the row for reconciliation.")
            try:
                document = json.loads(publication)
                # Reuse the wire admission path to validate retained inputs and identity.
                import base64
                document["outcome"] = {"api_origin": item.api_origin, "provider_ref": item.provider_ref,
                    "job_ref": item.job_ref, "execution_attempt_ref": item.execution_attempt_ref,
                    "operation": item.operation.value, "body": base64.b64encode(item.body).decode("ascii")}
                handoff = read_delivery_handoff(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii"))
                if handoff.publication_bytes() != publication:
                    raise ValueError("Noncanonical publication material")
                return handoff
            except (DeliveryHandoffRejected, TypeError, ValueError, UnicodeError):
                raise DeliveryStorageFailed("Cannot publish corrupted retained publication material; preserve the row for operator reconciliation.") from None

    def hold_legacy_rows(self):
        """Retain old rows without material while allowing other work to publish."""
        with self._transaction("hold legacy requests in"):
            return self._db.execute(
                "UPDATE outstanding SET state='held',held_reason=? WHERE publication IS NULL AND state='pending'",
                ("Original publication material is unavailable; retain for operator reconciliation.",),
            ).rowcount

    def next_pending(self, api_origin, provider_ref):
        with self._access("read"):
            row = self._db.execute(
                "SELECT job_ref,attempt_ref,operation,body,digest FROM outstanding WHERE api_origin=? AND provider_ref=? AND state='pending' ORDER BY created_at,attempt_ref LIMIT 1",
                (api_origin, provider_ref),
            ).fetchone()
            if row is None:
                return None
            try:
                item = DeliveryOutcome(api_origin, provider_ref, row[0], row[1], TerminalOperation(row[2]), row[3])
                if item.body_sha256 != row[4]:
                    raise ValueError("Stored delivery body does not match its retained SHA-256.")
            except ValueError as cause:
                raise DeliveryStorageFailed("Cannot publish corrupted delivery storage; preserve the row for operator reconciliation.", cause=cause) from None
            return item

    def outstanding_bindings(self):
        """Derive current obligations from rows; keep no separate binding registry."""
        with self._access("inspect binding obligations in"):
            return tuple(self._db.execute("SELECT DISTINCT api_origin,provider_ref FROM outstanding ORDER BY api_origin,provider_ref"))

    def inventory(self, api_origin, provider_ref):
        return self._inventory(api_origin, provider_ref, None, None)

    def ipc_inventory(self, api_origin, provider_ref):
        rows = self._inventory(api_origin, provider_ref, MAX_PROVIDER_ITEMS + 1, None)
        if len(rows) > MAX_PROVIDER_ITEMS:
            raise DeliveryInventoryUnavailable()
        return rows

    def status(self, api_origin, provider_ref, attempt_ref):
        """Read one bound Attempt without allocating a complete legacy inventory."""
        require_attempt_ref(attempt_ref)
        rows = self._inventory(api_origin, provider_ref, 1, attempt_ref)
        if rows:
            return rows[0]
        return None

    def _inventory(self, api_origin, provider_ref, limit, attempt_ref):
        with self._access("inspect"):
            query = "SELECT job_ref,attempt_ref,operation,digest,length(body),state,created_at,held_reason FROM outstanding WHERE api_origin=? AND provider_ref=?"
            parameters = (api_origin, provider_ref)
            if attempt_ref is not None:
                query += " AND attempt_ref=?"
                parameters += (attempt_ref,)
            query += " ORDER BY created_at,attempt_ref"
            if limit is not None:
                query += " LIMIT ?"
                parameters += (limit,)
            rows = self._db.execute(query, parameters).fetchall()
            return [
                {
                    "job_ref": job_ref,
                    "execution_attempt_ref": attempt_ref,
                    "operation": operation,
                    "body_sha256": digest,
                    "body_bytes": size,
                    "state": state,
                    "created_at": created,
                    "held_reason": reason,
                }
                for job_ref, attempt_ref, operation, digest, size, state, created, reason in rows
            ]

    def _match(self, item):
        return (*item.key, item.job_ref, item.operation.value, item.body)

    def remove_published(self, item):
        """Delete only after the API owner validated its matching terminal receipt.

        This internal method has no producer IPC endpoint. Receipt verification
        stays with ProviderAPI; the transaction protects exact queued identity.
        """
        with self._transaction("remove published data from"):
            cursor = self._db.execute("DELETE FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND job_ref=? AND operation=? AND body=?", self._match(item))
            if cursor.rowcount != 1:
                raise DeliveryConflict("The queued request changed before publication settlement; no different row was removed.")

    def hold(self, item, reason):
        """Retain a reviewed operator explanation, never raw remote response text."""
        if type(reason) is not str or not 1 <= len(reason) <= 512:
            raise ValueError("A held delivery requires a bounded operator explanation.")
        with self._transaction("hold a request in"):
            cursor = self._db.execute("UPDATE outstanding SET state='held',held_reason=? WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND job_ref=? AND operation=? AND body=?", (reason, *self._match(item)))
            if cursor.rowcount != 1:
                raise DeliveryConflict("The queued request changed before the hold; its current owner must reconcile it.")

    def discard_held(self, api_origin, provider_ref, attempt_ref, body_sha256):
        """Operator disposition only; removing held bytes proves no API receipt."""
        if _https_origin(api_origin)[0] != api_origin:
            raise ValueError("Discard requires the configured canonical API origin.")
        require_provider_ref(provider_ref)
        require_attempt_ref(attempt_ref)
        if type(body_sha256) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", body_sha256) is None:
            raise ValueError("Discard requires the held payload's exact SHA-256 from its metadata.")
        with self._transaction("discard a held request from"):
            cursor = self._db.execute(
                "DELETE FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND digest=? AND state='held'",
                (api_origin, provider_ref, attempt_ref, body_sha256),
            )
            if cursor.rowcount != 1:
                raise DeliveryConflict("No matching held payload was removed. Check its binding, Attempt and digest; pending requests cannot be discarded.")

    def close(self):
        with self._serial:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None
