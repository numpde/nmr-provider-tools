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
import reprlib
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from provider_core.attempt_record import TerminalOperation
from provider_core.validation.upstream.nmr.v1.attempt_request import read_complete_body, read_fail_body, require_provider_ref
from provider_core.validation.upstream.nmr.v1.attempt_response import require_attempt_ref
from provider_core.validation.upstream.nmr.v1.signing import https_origin
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.type_requirements import require_type


# Bound the shared daemon's supported fanout before granting per-binding state.
# repo-check: allow-module-constant
MAX_BINDINGS = 16

# The maximum escaped inventory row is 6604 bytes. This occupancy leaves over
# 396 KiB of the 2 MiB IPC response envelope for binding identity and status.
# repo-check: allow-module-constant
MAX_PROVIDER_ITEMS = 256

# repo-check: allow-module-constant
LEGACY_HOLD_REASON = "Original publication material is unavailable; retain for operator reconciliation."


class DeliveryConflict(RuntimeError):
    """An outstanding immutable request cannot be replaced by this submission."""


class DeliveryFull(RuntimeError):
    """No request was accepted; keep the producer's outcome and apply backpressure."""


class DeliveryInventoryUnavailable(RuntimeError):
    """An oversized legacy inventory cannot be sent as one bounded IPC reply."""

    def __init__(self):
        super().__init__(
            f"Pending inventory exceeds {MAX_PROVIDER_ITEMS=}; publication continues and all rows remain retained. "
            f"Read known Attempt status individually or retry inventory after the publisher drains the queue."
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
    provider_ref: str  # repo-check: allow-identifier-type -- API principal; require_provider_ref checks its syntax.
    job_ref: str  # repo-check: allow-identifier-type -- API Job identity; require_job_ref checks its syntax.
    execution_attempt_ref: str  # repo-check: allow-identifier-type -- API Attempt identity; require_attempt_ref checks its syntax.
    operation: TerminalOperation
    body: bytes = field(repr=False)

    def __post_init__(self):
        canonical_api_origin = https_origin(self.api_origin)[0]

        if not (canonical_api_origin == self.api_origin):
            raise ValueError(f"{reprlib.repr(self.api_origin)=} ≠ {reprlib.repr(canonical_api_origin)=}")

        require_provider_ref(self.provider_ref)
        require_job_ref(self.job_ref)
        require_attempt_ref(self.execution_attempt_ref)

        require_type(self.operation, TerminalOperation)

        parser = read_complete_body if self.operation is TerminalOperation.COMPLETE else read_fail_body

        parser(self.body, execution_attempt_ref=self.execution_attempt_ref)

    @property
    def key(self):
        return (self.api_origin, self.provider_ref, self.execution_attempt_ref)

    @property
    def body_sha256(self):
        return "sha256:" + hashlib.sha256(self.body).hexdigest()

    def acceptance(self):
        """Durable enqueue identity; this is not an API acceptance receipt."""

        return {
            "api_origin": self.api_origin,
            "provider_ref": self.provider_ref,
            "job_ref": self.job_ref,
            "execution_attempt_ref": self.execution_attempt_ref,
            "operation": self.operation.value,
            "body_sha256": self.body_sha256,
        }


@dataclass(frozen=True, slots=True)
class QueueLimits:
    provider_items: int
    provider_bytes: int
    total_items: int
    total_bytes: int

    def __post_init__(self):
        (
            provider_items,
            provider_bytes,
            total_items,
            total_bytes,
        ) = (
            self.provider_items,
            self.provider_bytes,
            self.total_items,
            self.total_bytes,
        )

        require_type(provider_items, int)
        require_type(provider_bytes, int)
        require_type(total_items, int)
        require_type(total_bytes, int)

        if not (0 < provider_items):
            raise ValueError(f"{provider_items=} should be positive")

        if not (0 < provider_bytes):
            raise ValueError(f"{provider_bytes=} should be positive")

        if not (0 < total_items):
            raise ValueError(f"{total_items=} should be positive")

        if not (0 < total_bytes):
            raise ValueError(f"{total_bytes=} should be positive")

        if not (provider_items <= MAX_PROVIDER_ITEMS):
            raise ValueError(f"{provider_items=} should be at most {MAX_PROVIDER_ITEMS=} to fit the pending inventory in one IPC response")

        if not (provider_items <= total_items):
            raise ValueError(f"{provider_items=} should be at most {total_items=}")

        if not (provider_bytes <= total_bytes):
            raise ValueError(f"{provider_bytes=} should be at most {total_bytes=}")


class DeliveryQueue:
    """The sole connection owns pending data; producers never open this database."""

    def __init__(self, directory, *, limits):
        require_type(limits, QueueLimits)

        (self.limits, self.directory) = (limits, directory)
        self._serial = threading.RLock()

        (self._db, self._lock, self._failure) = (None, None, None)

        try:
            directory_stat = directory.lstat()

            if not directory.is_absolute():
                raise DeliveryStorageFailed(f"{directory=} should be absolute")

            canonical_directory = directory.resolve()

            if not (canonical_directory == directory):
                raise DeliveryStorageFailed(f"{directory=} ≠ {canonical_directory=}")

            if not stat.S_ISDIR(directory_stat.st_mode):
                directory_file_type = stat.S_IFMT(directory_stat.st_mode)

                raise DeliveryStorageFailed(f"{directory=}; {directory_file_type=:#o} ≠ {stat.S_IFDIR=:#o}")

            effective_uid = os.geteuid()

            if not (directory_stat.st_uid == effective_uid):
                raise DeliveryStorageFailed(f"{directory=}; {directory_stat.st_uid=} ≠ {effective_uid=}")

            directory_permissions = stat.S_IMODE(directory_stat.st_mode)

            if not (directory_permissions == (required_directory_permissions := 0o700)):
                raise DeliveryStorageFailed(f"{directory=}; {directory_permissions=:#o} ≠ {required_directory_permissions=:#o}")

            self._lock = self._private_file(directory / ".owner.lock")

            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor = self._private_file(directory / "delivery.sqlite3")

            os.close(descriptor)
            self._db = sqlite3.connect(directory / "delivery.sqlite3", check_same_thread=False, timeout=1)

            self._db.execute(
                "PRAGMA journal_mode=DELETE"  # machine-string: SQLite selects the single-file journal mode required by delivery ownership
            )

            self._db.execute(
                "PRAGMA synchronous=EXTRA"  # machine-string: SQLite enforces the configured durable commit policy
            )

            self._db.execute(
                "PRAGMA secure_delete=ON"  # machine-string: SQLite erases deleted publication data as required by storage policy
            )

            self._initialize()
        except (OSError, sqlite3.Error, DeliveryStorageFailed) as cause:
            self.close()

            if type(cause) is DeliveryStorageFailed:
                raise

            raise DeliveryStorageFailed(f"Cannot open delivery storage at {directory=}", cause=cause) from None

    def _private_file(self, delivery_file_path):
        expected_file_mode = 0o600

        delivery_file_fd = os.open(delivery_file_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, expected_file_mode)

        try:
            try:
                file_metadata = os.fstat(delivery_file_fd)
            except OSError as cause:
                raise DeliveryStorageFailed(f"Cannot inspect {delivery_file_path=}", cause=cause) from None

            if not stat.S_ISREG(file_metadata.st_mode):
                file_type = stat.S_IFMT(file_metadata.st_mode)

                raise DeliveryStorageFailed(f"{delivery_file_path=}; {file_type=:#o} ≠ {stat.S_IFREG=:#o}")

            effective_uid = os.geteuid()

            if not (file_metadata.st_uid == effective_uid):
                raise DeliveryStorageFailed(f"{delivery_file_path=}; {file_metadata.st_uid=} ≠ {effective_uid=}")

            file_permissions = stat.S_IMODE(file_metadata.st_mode)

            if not (file_permissions == expected_file_mode):
                raise DeliveryStorageFailed(f"{delivery_file_path=}; {file_permissions=:#o} ≠ {expected_file_mode=:#o}")
        except BaseException:
            # Until return, this descriptor belongs here rather than self._lock
            # or SQLite. A close failure retains the admission failure as context.
            os.close(delivery_file_fd)

            raise

        return delivery_file_fd

    def _initialize(self):
        database_version = self._db.execute("PRAGMA user_version").fetchone()[0]

        if not (database_version in (supported_database_versions := {0, 1, 2})):
            raise DeliveryStorageFailed(f"{database_version=} ∉ {supported_database_versions=}")

        if database_version == 0:
            first_existing_table = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()

            if not (first_existing_table is None):
                raise DeliveryStorageFailed(f"{reprlib.repr(first_existing_table)=} should be None before initializing delivery storage")

            with self._transaction("initialize"):
                self._db.execute(
                    """CREATE TABLE outstanding (
                    api_origin TEXT NOT NULL, provider_ref TEXT NOT NULL, attempt_ref TEXT NOT NULL,
                    job_ref TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('complete','fail')),
                    body BLOB NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','held')),
                    created_at REAL NOT NULL, held_reason TEXT,
                    PRIMARY KEY(api_origin, provider_ref, attempt_ref))"""
                )

                self._db.execute(
                    "PRAGMA user_version=1"  # machine-string: SQLite records the initialized delivery schema version
                )

            database_version = 1

        if database_version == 1:
            with self._transaction("upgrade"):
                self._db.execute("ALTER TABLE outstanding ADD COLUMN publication BLOB")

                self._db.execute(
                    "PRAGMA user_version=2"  # machine-string: SQLite records the migrated delivery schema version
                )

        # Detect incompatible/corrupt schema before serving any publication.
        self._db.execute("SELECT api_origin,provider_ref,attempt_ref,job_ref,operation,body,digest,state,created_at,held_reason,publication FROM outstanding LIMIT 0")

    @contextmanager
    def _access(self, operation):
        with self._serial:
            if not (self._failure is None):
                raise self._failure

            if not (self._db is not None):
                raise DeliveryStorageFailed(f"{self._db=} should be open for {operation=}")

            try:
                yield
            except sqlite3.Error as cause:
                self._failure = DeliveryStorageFailed(
                    f"Cannot access delivery storage during {operation=}. Preserve the database and restore storage before continuing; earlier delivery acknowledgements and API receipts are unchanged.",
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

        require_type(handoff, DeliveryHandoff)

        return self._enqueue(handoff.outcome, handoff.publication_bytes(), handoff.acceptance())

    def _enqueue(self, delivery_outcome, publication, acceptance):
        with self._transaction("enqueue into"):
            limits = self.limits

            existing = (
                self._db
                .execute(
                    "SELECT job_ref,operation,body,publication FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=?",
                    delivery_outcome.key,
                )
                .fetchone()
            )

            if existing is not None:
                if existing != (delivery_outcome.job_ref, delivery_outcome.operation.value, delivery_outcome.body, publication):
                    raise DeliveryConflict(f"{delivery_outcome.execution_attempt_ref=} already has a different outstanding terminal request")

                return acceptance

            for (queue_scope, where, parameters, maximum_items, maximum_bytes) in (
                ("provider", " WHERE api_origin=? AND provider_ref=?", delivery_outcome.key[:2], limits.provider_items, limits.provider_bytes),
                ("host", "", (), limits.total_items, limits.total_bytes),
            ):
                (queued_items, queued_body_bytes) = self._db.execute("SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM outstanding" + where, parameters).fetchone()

                if not (queued_items < maximum_items):
                    raise DeliveryFull(f"{queue_scope=}; {queued_items=} should be below {maximum_items=}")

                required_body_bytes = queued_body_bytes + len(delivery_outcome.body)

                if not (required_body_bytes <= maximum_bytes):
                    raise DeliveryFull(f"{queue_scope=}; {required_body_bytes=} should not exceed {maximum_bytes=}")

            self._db.execute(
                "INSERT INTO outstanding (api_origin,provider_ref,attempt_ref,job_ref,operation,body,digest,state,created_at,held_reason,publication) VALUES (?,?,?,?,?,?,?,'pending',?,NULL,?)",
                (*delivery_outcome.key, delivery_outcome.job_ref, delivery_outcome.operation.value, delivery_outcome.body, delivery_outcome.body_sha256, time.time(), publication)
            )

        return acceptance

    def next_pending_handoff(self, api_origin, provider_ref):
        """Return the oldest publishable row with its own original signing material."""
        from provider_core.delivery_handoff import DeliveryHandoffRejected, read_delivery_handoff

        with self._access("read a publication from"):
            pending_outcome = self.next_pending(api_origin, provider_ref)

            if pending_outcome is None:
                return None

            publication_material_bytes = (
                self._db
                .execute(
                    "SELECT publication FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=?",
                    pending_outcome.key,
                )
                .fetchone()[0]
            )

            if not (publication_material_bytes is not None):
                raise DeliveryStorageFailed(f"Cannot publish {pending_outcome.execution_attempt_ref=} with {publication_material_bytes=}; its row remains retained")

            try:
                handoff_fields = json.loads(publication_material_bytes)

                # Reuse the wire admission path to validate retained inputs and identity.
                import base64

                handoff_fields["outcome"] = {
                    "api_origin": pending_outcome.api_origin,
                    "provider_ref": pending_outcome.provider_ref,
                    "job_ref": pending_outcome.job_ref,
                    "execution_attempt_ref": pending_outcome.execution_attempt_ref,
                    "operation": pending_outcome.operation.value,
                    "body": base64.b64encode(pending_outcome.body).decode("ascii")
                }

                handoff = read_delivery_handoff(json.dumps(handoff_fields, sort_keys=True, separators=(",", ":")).encode("ascii"))

                canonical_publication_bytes = handoff.publication_bytes()

                if not (canonical_publication_bytes == publication_material_bytes):
                    canonical_publication_sha256 = hashlib.sha256(canonical_publication_bytes).hexdigest()
                    stored_publication_sha256 = hashlib.sha256(publication_material_bytes).hexdigest()

                    raise ValueError(f"Publication bytes differ with {canonical_publication_sha256=}, {stored_publication_sha256=}")

                return handoff
            except (DeliveryHandoffRejected, TypeError, ValueError, UnicodeError) as cause:
                raise DeliveryStorageFailed(f"Cannot restore publication material for {pending_outcome.execution_attempt_ref=}; its row remains retained", cause=cause) from None

    def hold_legacy_rows(self):
        """Retain old rows without material while allowing other work to publish."""

        with self._transaction("hold legacy requests in"):
            return self._db.execute(
                "UPDATE outstanding SET state='held',held_reason=? WHERE publication IS NULL AND state='pending'",
                (LEGACY_HOLD_REASON,),
            ).rowcount

    def next_pending(self, api_origin, provider_ref):
        with self._access("read"):
            row = (
                self._db
                .execute(
                    "SELECT job_ref,attempt_ref,operation,body,digest FROM outstanding WHERE api_origin=? AND provider_ref=? AND state='pending' ORDER BY created_at,attempt_ref LIMIT 1",
                    (api_origin, provider_ref),
                )
                .fetchone()
            )

            if row is None:
                return None

            (job_ref, attempt_ref, operation_name, body_bytes, stored_body_sha256) = row

            try:
                delivery_outcome = DeliveryOutcome(api_origin, provider_ref, job_ref, attempt_ref, TerminalOperation(operation_name), body_bytes)

                if not (delivery_outcome.body_sha256 == stored_body_sha256):
                    raise ValueError(f"{delivery_outcome.body_sha256=} ≠ {reprlib.repr(stored_body_sha256)=}")
            except (TypeError, ValueError) as cause:
                pending_identity = (api_origin, provider_ref, attempt_ref)

                raise DeliveryStorageFailed(f"Cannot restore {reprlib.repr(pending_identity)=}; its row remains retained", cause=cause) from None

            return delivery_outcome

    def outstanding_bindings(self):
        """Derive current obligations from rows; keep no separate binding registry."""

        with self._access("inspect binding obligations in"):
            return tuple(self._db.execute("SELECT DISTINCT api_origin,provider_ref FROM outstanding ORDER BY api_origin,provider_ref"))

    def pending_bindings(self):
        with self._access("inspect pending publication in"):
            return tuple(self._db.execute("SELECT DISTINCT api_origin,provider_ref FROM outstanding WHERE state='pending' ORDER BY api_origin,provider_ref"))

    def pending_overview(self):
        """Observe aggregate queue usage with the pending publisher identities."""

        with self._access("inspect pending publication in"):
            bindings = tuple(self._db.execute("SELECT DISTINCT api_origin,provider_ref FROM outstanding WHERE state='pending' ORDER BY api_origin,provider_ref"))
            (overview, failure) = (None, None)

            try:
                (pending, held, bodies, material, oldest) = (
                    self._db
                    .execute(
                        """
                    SELECT COALESCE(SUM(state='pending'),0), COALESCE(SUM(state='held'),0),
                           COALESCE(SUM(length(body)),0), COALESCE(SUM(length(publication)),0),
                           MIN(CASE WHEN state='pending' THEN created_at END)
                    FROM outstanding
                """,
                    )
                    .fetchone()
                )

                overview = {
                    "pending": pending,
                    "held": held,
                    "body_bytes": bodies,
                    "publication_bytes": material,
                    "oldest_pending_created_at": oldest
                }
            except sqlite3.Error as error:
                # Do not carry SQLite text, traceback or causal context across the diagnostic boundary.
                failure = type(error)()

                code = getattr(error, "sqlite_errorcode", None)

                if type(code) is int:
                    failure.sqlite_errorcode = code

            return (bindings, overview, failure)

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

    def _inventory(self, api_origin, provider_ref, max_rows, attempt_ref):
        with self._access("inspect"):
            query = "SELECT job_ref,attempt_ref,operation,digest,length(body),state,created_at,held_reason FROM outstanding WHERE api_origin=? AND provider_ref=?"
            parameters = (api_origin, provider_ref)

            if attempt_ref is not None:
                query += " AND attempt_ref=?"
                parameters += (attempt_ref,)

            query += " ORDER BY created_at,attempt_ref"

            if max_rows is not None:
                query += " LIMIT ?"
                parameters += (max_rows,)

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
                for (job_ref, attempt_ref, operation, digest, size, state, created, reason) in rows
            ]

    def _match(self, delivery_outcome):
        return (*delivery_outcome.key, delivery_outcome.job_ref, delivery_outcome.operation.value, delivery_outcome.body)

    def remove_published(self, item):
        """Delete only after the API owner validated its matching terminal receipt.

        This internal method has no producer IPC endpoint. Receipt verification
        stays with ProviderAPI; the transaction protects exact queued identity.
        """

        with self._transaction("remove published data from"):
            cursor = self._db.execute("DELETE FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND job_ref=? AND operation=? AND body=?", self._match(item))

            if not (cursor.rowcount == (expected_removed_rows := 1)):
                raise DeliveryConflict(f"{cursor.rowcount=} ≠ {expected_removed_rows=}; no different request was removed")

    def hold(self, item, reason):
        """Retain a reviewed operator explanation, never raw remote response text."""

        require_type(reason, str)

        if not (1 <= len(reason) <= (max_hold_reason_characters := 512)):
            raise ValueError(f"{len(reason)=} should be positive and not exceed {max_hold_reason_characters=}")

        with self._transaction("hold a request in"):
            cursor = self._db.execute("UPDATE outstanding SET state='held',held_reason=? WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND job_ref=? AND operation=? AND body=?", (reason, *self._match(item)))

            if not (cursor.rowcount == (expected_held_rows := 1)):
                raise DeliveryConflict(f"{cursor.rowcount=} ≠ {expected_held_rows=}; the queued request no longer matches")

    def discard_held(self, api_origin, provider_ref, attempt_ref, body_sha256):
        """Operator disposition only; removing held bytes proves no API receipt."""

        canonical_api_origin = https_origin(api_origin)[0]

        if not (canonical_api_origin == api_origin):
            raise ValueError(f"{reprlib.repr(api_origin)=} ≠ {reprlib.repr(canonical_api_origin)=}")

        require_provider_ref(provider_ref)
        require_attempt_ref(attempt_ref)

        require_type(body_sha256, str)

        if not (re.fullmatch(body_hash_pattern := r"sha256:[0-9a-f]{64}", body_sha256) is not None):
            raise ValueError(f"{reprlib.repr(body_sha256)=} should match {body_hash_pattern=}")

        with self._transaction("discard a held request from"):
            cursor = self._db.execute(
                "DELETE FROM outstanding WHERE api_origin=? AND provider_ref=? AND attempt_ref=? AND digest=? AND state='held'",
                (api_origin, provider_ref, attempt_ref, body_sha256),
            )

            if not (cursor.rowcount == (expected_removed_rows := 1)):
                raise DeliveryConflict(f"{cursor.rowcount=} ≠ {expected_removed_rows=}; no matching held request was removed")

    def close(self):
        with self._serial:
            if self._db is not None:
                self._db.close()
                self._db = None

            if self._lock is not None:
                os.close(self._lock)
                self._lock = None
