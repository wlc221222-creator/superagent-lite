"""SQLite persistence for tickets."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Ticket, TicketStatus, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT PRIMARY KEY,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL,
    worker         TEXT NOT NULL,
    workdir        TEXT NOT NULL,
    timeout        REAL NOT NULL,
    result_summary TEXT,
    artifacts      TEXT,
    depends_on     TEXT,
    log_path       TEXT,
    error          TEXT,
    exit_code      INTEGER,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    claim_token    TEXT,
    origin         TEXT NOT NULL DEFAULT 'cli',
    review_status  TEXT NOT NULL DEFAULT 'none',
    review_reasons TEXT,
    review_attempts INTEGER NOT NULL DEFAULT 0,
    approval_hash  TEXT,
    task_id        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    started_at     TEXT,
    heartbeat_at   TEXT,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
"""

FIELDS: tuple[str, ...] = (
    "id",
    "description",
    "status",
    "worker",
    "workdir",
    "timeout",
    "result_summary",
    "artifacts",
    "depends_on",
    "log_path",
    "error",
    "exit_code",
    "retry_count",
    "max_retries",
    "attempts",
    "claim_token",
    "origin",
    "review_status",
    "review_reasons",
    "review_attempts",
    "approval_hash",
    "task_id",
    "created_at",
    "updated_at",
    "started_at",
    "heartbeat_at",
    "finished_at",
)

# Columns added after the first MVP schema; existing databases are migrated.
# Additive and idempotent (guarded by PRAGMA table_info); every phase-5 column
# carries a NOT NULL default so legacy rows never hold NULL gate inputs.
_ADDED_COLUMNS: dict[str, str] = {
    "artifacts": "TEXT",
    "depends_on": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "heartbeat_at": "TEXT",
    "claim_token": "TEXT",
    "origin": "TEXT NOT NULL DEFAULT 'legacy'",
    "review_status": "TEXT NOT NULL DEFAULT 'none'",
    "review_reasons": "TEXT",
    "review_attempts": "INTEGER NOT NULL DEFAULT 0",
    "approval_hash": "TEXT",
    "task_id": "TEXT",
}

# Fields that callers may update through ``set_status``.
UPDATABLE_FIELDS = frozenset(FIELDS) - {"id", "created_at"}

# Sentinel for "the caller did not supply a lease token".  ``None`` is a real
# token value (unclaimed legacy rows), so it cannot double as the sentinel.
_UNSET = object()


class TicketStore:
    """Small SQLite-backed ticket store.

    One connection is kept per instance; the CLI is single-process and
    single-threaded, which suits the MVP.  Writes are committed immediately so
    a process restart never loses a completed ticket.
    """

    def __init__(
        self,
        path: str | Path = "runtime/superagent.db",
        *,
        stale_seconds: float = 300.0,
        recover: bool = True,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # The heartbeat ticker writes from a background thread while the
        # router reads/writes from the main one; serialize access explicitly.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        #: Set when ``_migrate`` copied an existing database before ALTERing.
        self.backup_path: str | None = None
        self._migrate()
        self._conn.commit()
        self._closed = False
        self.recovered: list[Ticket] = []
        if recover:
            self.recovered = self.recover_stale_running(stale_seconds=stale_seconds)

    def _migrate(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        missing = [
            (column, declaration)
            for column, declaration in _ADDED_COLUMNS.items()
            if column not in existing
        ]
        if missing:
            # An older build's database lacks phase-5 columns.  Take the one
            # backup copy before the additive ALTERs (Revision 4).  A fresh
            # database has every column already, so nothing is copied.
            self.backup_path = self._backup_before_migration()
        for column, declaration in missing:
            self._conn.execute(
                f"ALTER TABLE tickets ADD COLUMN {column} {declaration}"
            )

    def _backup_before_migration(self) -> str | None:
        """Copy the DB file once before the additive ALTERs; return its path."""

        if self.path == ":memory:":
            return None
        source = Path(self.path)
        if not source.is_file() or source.stat().st_size == 0:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = source.with_name(f"{source.name}.pre-phase5-{stamp}.bak")
        suffix = 0
        while backup.exists():  # pragma: no cover - same-second collision
            suffix += 1
            backup = source.with_name(
                f"{source.name}.pre-phase5-{stamp}-{suffix}.bak"
            )
        shutil.copy2(source, backup)
        return str(backup)

    # -- writes ---------------------------------------------------------
    def create(self, ticket: Ticket) -> Ticket:
        placeholders = ", ".join("?" for _ in FIELDS)
        columns = ", ".join(FIELDS)
        values = tuple(_value(getattr(ticket, name)) for name in FIELDS)
        with self._lock:
            try:
                self._conn.execute(
                    f"INSERT INTO tickets ({columns}) VALUES ({placeholders})", values
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"ticket already exists: {ticket.id}") from exc
            self._conn.commit()
        return ticket

    def save(self, ticket: Ticket, *, expected_claim_token: object = _UNSET) -> Ticket | None:
        """Persist every mutable field of ``ticket``.

        With ``expected_claim_token`` the write is a compare-and-swap: it only
        applies while the row is still ``running`` under that exact lease and
        returns ``None`` otherwise.  This is what stops a stale execution
        from overwriting a ticket that was requeued and claimed again.
        """

        assignments = ", ".join(f"{name} = ?" for name in FIELDS if name != "id")
        values = tuple(
            _value(getattr(ticket, name)) for name in FIELDS if name != "id"
        ) + (ticket.id,)
        query = f"UPDATE tickets SET {assignments} WHERE id = ?"
        with self._lock:
            if expected_claim_token is not _UNSET:
                query += " AND status = ? AND claim_token IS ?"
                values += (TicketStatus.RUNNING.value, expected_claim_token)
            cursor = self._conn.execute(query, values)
            if cursor.rowcount == 0:
                self._conn.rollback()
                if expected_claim_token is not _UNSET:
                    return None
                raise KeyError(f"ticket not found: {ticket.id}")
            self._conn.commit()
        return ticket

    def set_status(
        self,
        ticket_id: str,
        status: TicketStatus | str,
        *,
        touch: bool = True,
        expected_claim_token: object = _UNSET,
        **fields: object,
    ) -> Ticket | None:
        """Update a ticket's status (plus optional fields) atomically.

        ``touch`` refreshes ``updated_at``; broadcast-only writes such as a
        heartbeat pass ``touch=False`` so the row's mtime keeps meaning
        "last real state change".

        ``expected_claim_token`` makes the update conditional on the ticket
        still being ``running`` under that lease; ``None`` is returned when
        the lease was lost.
        """

        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"cannot update unknown ticket fields: {sorted(unknown)}")

        # One lock span covers read-modify-write, so a heartbeat from another
        # thread cannot interleave between the read and the save.
        with self._lock:
            ticket = self.get(ticket_id)
            if ticket is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            if expected_claim_token is not _UNSET:
                if (
                    ticket.status is not TicketStatus.RUNNING
                    or ticket.claim_token != expected_claim_token
                ):
                    return None
            ticket.status = TicketStatus(status)
            for name, value in fields.items():
                setattr(ticket, name, value)
            if touch:
                ticket.updated_at = utc_now()
            return self.save(ticket, expected_claim_token=expected_claim_token)

    def claim(
        self, ticket_id: str, *, max_retries: int | None = None
    ) -> Ticket | None:
        """Atomically claim one specific pending ticket.

        Returns the freshly claimed (running) ticket, or ``None`` when the
        ticket is not pending any more -- for example because a daemon won
        the race.  A ticket whose ``depends_on`` prerequisites are not all
        ``done`` is never claimed.
        """

        now = utc_now()
        token = secrets.token_hex(8)
        with self._lock:
            pending = self.get(ticket_id)
            if pending is None or pending.status is not TicketStatus.PENDING:
                return None
            if not self._dependencies_done(pending.depends_on):
                # Fail-fast: a dependency that already failed can never be
                # satisfied, so the ticket is failed now rather than sitting
                # pending forever.
                self._fail_blocked_by_dependency(ticket_id, pending.depends_on)
                return None
            cursor = self._conn.execute(
                "UPDATE tickets SET status = ?, started_at = ?, heartbeat_at = ?, "
                "updated_at = ?, claim_token = ?, attempts = attempts + 1, "
                "error = NULL, exit_code = NULL, finished_at = NULL "
                "WHERE id = ? AND status = ?",
                (
                    TicketStatus.RUNNING.value,
                    now,
                    now,
                    now,
                    token,
                    ticket_id,
                    TicketStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return None
            if max_retries is not None and int(max_retries) >= 0:
                self._conn.execute(
                    "UPDATE tickets SET max_retries = ? "
                    "WHERE id = ? AND max_retries < ?",
                    (int(max_retries), ticket_id, int(max_retries)),
                )
            self._conn.commit()
        return self.get(ticket_id)

    def claim_next(
        self,
        *,
        workers: Iterable[str] | None = None,
        max_retries: int | None = None,
    ) -> Ticket | None:
        """Atomically claim the oldest pending ticket and mark it running.

        The claim is a single ``UPDATE ... WHERE id = ? AND status =
        'pending'``; ``rowcount == 1`` means this process won the race.  A
        fresh ``claim_token`` is written so every later heartbeat and result
        can be checked against this exact lease.  Returns ``None`` when the
        queue is empty (or another process claimed the row first).
        """

        names = list(workers) if workers is not None else None
        if names is not None and not names:
            return None
        now = utc_now()
        with self._lock:
            query = "SELECT id, depends_on FROM tickets WHERE status = ?"
            params: list[object] = [TicketStatus.PENDING.value]
            if names is not None:
                query += f" AND worker IN ({', '.join('?' for _ in names)})"
                params.extend(names)
            query += " ORDER BY created_at ASC, rowid ASC"
            rows = self._conn.execute(query, params).fetchall()
            ticket_id: str | None = None
            for row in rows:
                dependencies = self._parse_dependencies(row["depends_on"])
                if self._dependencies_done(dependencies):
                    ticket_id = row["id"]
                    break
                # A dependency that ended ``failed`` is unsatisfiable: fail
                # this ticket now instead of leaving it pending forever.
                self._fail_blocked_by_dependency(row["id"], dependencies)
            if ticket_id is None:
                return None
            token = secrets.token_hex(8)
            cursor = self._conn.execute(
                "UPDATE tickets SET status = ?, started_at = ?, heartbeat_at = ?, "
                "updated_at = ?, claim_token = ?, attempts = attempts + 1, "
                "error = NULL, exit_code = NULL, finished_at = NULL "
                "WHERE id = ? AND status = ?",
                (
                    TicketStatus.RUNNING.value,
                    now,
                    now,
                    now,
                    token,
                    ticket_id,
                    TicketStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return None
            if max_retries is not None and int(max_retries) >= 0:
                self._conn.execute(
                    "UPDATE tickets SET max_retries = ? "
                    "WHERE id = ? AND max_retries < ?",
                    (int(max_retries), ticket_id, int(max_retries)),
                )
            self._conn.commit()
        return self.get(ticket_id)

    def fail_dependents(
        self, ticket_id: str, *, reason: str | None = None
    ) -> list[Ticket]:
        """Fail every pending ticket that depends on the failed ``ticket_id``.

        A dependency that ends ``failed`` can never be satisfied, so leaving
        its dependents ``pending`` forever would silently park work.  Only
        ``pending`` tickets are touched: one that is already running (it was
        claimed while the dependency looked done) is left to its own result.
        The cascade is one level deep; callers may invoke it again for each
        newly failed ticket.
        """

        message = reason or f"dependency failed: {ticket_id}"
        failed: list[Ticket] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, depends_on FROM tickets WHERE status IN (?, ?)",
                (
                    TicketStatus.PENDING.value,
                    TicketStatus.AWAITING_APPROVAL.value,
                ),
            ).fetchall()
            for row in rows:
                if ticket_id not in self._parse_dependencies(row["depends_on"]):
                    continue
                updated = self.set_status(
                    row["id"],
                    TicketStatus.FAILED,
                    error=message,
                    finished_at=utc_now(),
                )
                failed.append(updated)
        return failed

    # -- approval gate --------------------------------------------------
    def approve(self, ticket_id: str) -> tuple[Ticket | None, str]:
        """Move ``awaiting_approval`` -> ``pending``.

        Compare-and-swap on the status, plus the content-hash check: if the
        description no longer matches what the approver was shown, the
        approval is refused.  Returns ``(ticket, "")`` or ``(None, reason)``.
        """

        with self._lock:
            ticket = self.get(ticket_id)
            error = self._approval_blocker(ticket_id, ticket)
            if error:
                return None, error
            assert ticket is not None
            if ticket.approval_hash and (
                approval_content_hash(ticket.description) != ticket.approval_hash
            ):
                return None, (
                    "description changed since the approval request; "
                    "refusing to approve stale content"
                )
            if not self._cas_approval(
                ticket_id, TicketStatus.PENDING, "approval_hash = NULL"
            ):
                return None, f"ticket {ticket_id} is no longer awaiting_approval"
        return self.get(ticket_id), ""

    def reject(
        self, ticket_id: str, *, reason: str = "rejected by approver"
    ) -> tuple[Ticket | None, str]:
        """Move ``awaiting_approval`` -> ``failed`` (``rejected by approver``)."""

        with self._lock:
            ticket = self.get(ticket_id)
            error = self._approval_blocker(ticket_id, ticket)
            if error:
                return None, error
            if not self._cas_approval(
                ticket_id,
                TicketStatus.FAILED,
                "error = ?, finished_at = ?, approval_hash = NULL",
                (reason, utc_now()),
            ):
                return None, f"ticket {ticket_id} is no longer awaiting_approval"
        return self.get(ticket_id), ""

    def _cas_approval(
        self, ticket_id: str, status: TicketStatus, assignments: str, extra: tuple = ()
    ) -> bool:
        """``UPDATE ... WHERE id=? AND status='awaiting_approval'``; rowcount is the CAS.

        The check and the write are one statement, so two concurrent
        ``approve`` calls (CLI + HTTP) cannot both win: the loser sees
        ``rowcount == 0`` and reports "not awaiting approval".
        """

        cursor = self._conn.execute(
            f"UPDATE tickets SET status = ?, updated_at = ?, {assignments} "
            "WHERE id = ? AND status = ?",
            (
                status.value,
                utc_now(),
                *extra,
                ticket_id,
                TicketStatus.AWAITING_APPROVAL.value,
            ),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            return False
        self._conn.commit()
        return True

    @staticmethod
    def _approval_blocker(ticket_id: str, ticket: Ticket | None) -> str:
        """Why this ticket cannot be approved/rejected right now (or "")."""

        if ticket is None:
            return f"ticket not found: {ticket_id}"
        if ticket.status is not TicketStatus.AWAITING_APPROVAL:
            return (
                f"ticket {ticket_id} is {ticket.status.value}, "
                "not awaiting_approval"
            )
        return ""

    def heartbeat(
        self, ticket_id: str, *, claim_token: object = _UNSET
    ) -> Ticket | None:
        """Refresh ``heartbeat_at`` for a running ticket.

        Returns ``None`` when the ticket is gone, no longer running, or owned
        by a different lease (for example because this worker was presumed
        dead and its ticket requeued).
        """

        now = utc_now()
        query = (
            "UPDATE tickets SET heartbeat_at = ? WHERE id = ? AND status = ?"
        )
        params: list[object] = [now, ticket_id, TicketStatus.RUNNING.value]
        if claim_token is not _UNSET:
            query += " AND claim_token IS ?"
            params.append(claim_token)
        with self._lock:
            cursor = self._conn.execute(query, params)
            changed = cursor.rowcount == 1
            self._conn.commit()
        return self.get(ticket_id) if changed else None

    def requeue(
        self,
        ticket_id: str,
        *,
        claim_token: object = _UNSET,
        reason: str,
        force: bool = False,
    ) -> tuple[Ticket | None, bool]:
        """Put an interrupted running ticket back on the queue.

        Returns ``(ticket, requeued)``.  The ticket goes back to ``pending``
        while it still has retries left; once ``retry_count`` reaches
        ``max_retries`` it is failed instead with ``reason``.  A ticket whose
        lease no longer matches is left untouched (``(None, False)``).

        ``force=True`` requeues even when the retry budget is spent.  The
        daemon uses it on graceful shutdown: an operator-initiated SIGTERM is
        not the ticket's fault, so the work must not be lost.
        """

        with self._lock:
            ticket = self.get(ticket_id)
            if ticket is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            if ticket.status is not TicketStatus.RUNNING:
                return None, False
            if claim_token is not _UNSET and ticket.claim_token != claim_token:
                return None, False

            if force or ticket.retry_count < ticket.max_retries:
                updated = self.set_status(
                    ticket_id,
                    TicketStatus.PENDING,
                    retry_count=ticket.retry_count + 1,
                    claim_token=None,
                    started_at=None,
                    heartbeat_at=None,
                    result_summary=None,
                    error=reason,
                )
                _append_recovery_note(ticket.log_path, f"requeued: {reason}")
                return updated, True
            updated = self.set_status(
                ticket_id,
                TicketStatus.FAILED,
                claim_token=None,
                error=reason,
                finished_at=utc_now(),
            )
            _append_recovery_note(ticket.log_path, f"not requeued: {reason}")
            return updated, False

    def recover_stale_running(
        self,
        *,
        stale_seconds: float = 300.0,
        reason: str | None = None,
        requeue: bool = False,
        started_before: datetime | None = None,
    ) -> list[Ticket]:
        """Recover ``running`` tickets whose heartbeat went stale.

        The heartbeat is the liveness signal: a row whose ``heartbeat_at`` is
        recent (a live worker in another process) is left alone.  A running
        row that never recorded a heartbeat is stale by definition, which
        also covers rows written by the pre-heartbeat MVP.

        By default a stale ticket is failed (iteration 2 behaviour).  With
        ``requeue=True`` it is put back on the queue while it has retries
        left, and failed otherwise -- the daemon's watchdog path.  A fresh
        ticket that never got a heartbeat within ``stale_seconds`` of being
        claimed is only recovered once its claim itself is old enough, so a
        daemon restart does not steal a ticket that another live daemon
        claimed moments ago (``started_before`` narrows that further).
        """

        threshold = float(stale_seconds)
        now = datetime.now(timezone.utc)
        recovered: list[Ticket] = []
        for ticket in self.list_tickets(status=TicketStatus.RUNNING, limit=None):
            started = _parse_ts(ticket.started_at)
            if started_before is not None and started is not None and started >= started_before:
                continue
            stamp = _parse_ts(ticket.heartbeat_at)
            reference = stamp or started
            if reference is not None and (now - reference).total_seconds() < threshold:
                continue
            if reference is None:
                age = "none recorded"
            else:
                age = f"{int((now - reference).total_seconds())}s"
            message = reason or (
                "interrupted: heartbeat "
                f"{age} (threshold {threshold:g}s); process likely died"
            )
            if requeue:
                recovered_ticket, _ = self.requeue(ticket.id, reason=message)
                if recovered_ticket is not None:
                    recovered.append(recovered_ticket)
                continue
            recovered_ticket = self.set_status(
                ticket.id,
                TicketStatus.FAILED,
                claim_token=None,
                error=message,
                finished_at=utc_now(),
            )
            _append_recovery_note(recovered_ticket.log_path, message)
            recovered.append(recovered_ticket)
        return recovered

    # -- reads ----------------------------------------------------------
    def _parse_dependencies(self, raw: object) -> list[str]:
        """Decode a ``depends_on`` column.

        A row written by an older build has no ``depends_on`` column value
        (the migrated column is NULL), which decodes to "no dependencies".
        """

        try:
            return _string_list(raw)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return []

    def _failed_dependency(self, ticket_ids: list[str]) -> str | None:
        """Return the first dependency that is already ``failed``, if any."""

        for dependency in ticket_ids:
            row = self._conn.execute(
                "SELECT status FROM tickets WHERE id = ?", (dependency,)
            ).fetchone()
            if row is not None and row["status"] == TicketStatus.FAILED.value:
                return str(dependency)
        return None

    def _fail_blocked_by_dependency(
        self, ticket_id: str, ticket_ids: list[str]
    ) -> Ticket | None:
        """Fail ``ticket_id`` when one of its dependencies already failed."""

        failed_dependency = self._failed_dependency(ticket_ids)
        if failed_dependency is None:
            return None
        return self.set_status(
            ticket_id,
            TicketStatus.FAILED,
            error=f"dependency failed: {failed_dependency}",
            finished_at=utc_now(),
        )

    def _dependencies_done(self, ticket_ids: list[str]) -> bool:
        """True when every id in ``ticket_ids`` exists and is ``done``.

        Unknown ids count as unsatisfied: a dependency on a ticket that was
        never created can only be a planning mistake, and the ticket must not
        run on an assumption the planner got wrong.  A dependency that already
        failed is fail-fast: the claim path fails the blocked ticket instead of
        leaving it pending forever (see :meth:`_fail_blocked_by_dependency`).
        """

        for dependency in ticket_ids:
            row = self._conn.execute(
                "SELECT status FROM tickets WHERE id = ?", (dependency,)
            ).fetchone()
            if row is None or row["status"] != TicketStatus.DONE.value:
                return False
        return True

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(FIELDS)} FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_tickets(
        self,
        status: TicketStatus | str | None = None,
        limit: int | None = 50,
    ) -> list[Ticket]:
        query = f"SELECT {', '.join(FIELDS)} FROM tickets"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(TicketStatus(status).value)
        query += " ORDER BY created_at DESC, rowid DESC"
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be greater than 0")
            query += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows: Iterable[sqlite3.Row] = self._conn.execute(query, params).fetchall()
        return [_from_row(row) for row in rows]

    def count(self, status: TicketStatus | str | None = None) -> int:
        with self._lock:
            if status is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM tickets WHERE status = ?",
                    (TicketStatus(status).value,),
                ).fetchone()
        return int(row["n"])

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> TicketStore:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def approval_content_hash(description: str) -> str:
    """sha256 of a ticket description, the value the gate records."""

    return hashlib.sha256(str(description).encode("utf-8")).hexdigest()


def _value(value: object) -> object:
    if isinstance(value, TicketStatus):
        return value.value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


def _string_list(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _artifacts(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _append_recovery_note(log_path: str | None, message: str) -> None:
    """Leave an audit trail in the ticket log when recovering a stale row."""

    if not log_path:
        return
    from .logs import append_log

    append_log(log_path, f"[{utc_now()}] recovered: {message}\n")


def _from_row(row: sqlite3.Row) -> Ticket:
    return Ticket(
        id=row["id"],
        description=row["description"],
        status=TicketStatus(row["status"]),
        worker=row["worker"],
        workdir=row["workdir"],
        timeout=float(row["timeout"]),
        result_summary=row["result_summary"],
        artifacts=_artifacts(row["artifacts"]),
        depends_on=_string_list(row["depends_on"]),
        log_path=row["log_path"],
        error=row["error"],
        exit_code=row["exit_code"],
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        attempts=int(row["attempts"] or 0),
        claim_token=row["claim_token"],
        origin=row["origin"] or "legacy",
        review_status=row["review_status"] or "none",
        review_reasons=_string_list(row["review_reasons"]),
        review_attempts=int(row["review_attempts"] or 0),
        approval_hash=row["approval_hash"],
        task_id=row["task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        finished_at=row["finished_at"],
    )
