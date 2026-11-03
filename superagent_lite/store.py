"""SQLite persistence for tickets."""

from __future__ import annotations

import json
import secrets
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
    log_path       TEXT,
    error          TEXT,
    exit_code      INTEGER,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    claim_token    TEXT,
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
    "log_path",
    "error",
    "exit_code",
    "retry_count",
    "max_retries",
    "attempts",
    "claim_token",
    "created_at",
    "updated_at",
    "started_at",
    "heartbeat_at",
    "finished_at",
)

# Columns added after the first MVP schema; existing databases are migrated.
_ADDED_COLUMNS: dict[str, str] = {
    "artifacts": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "heartbeat_at": "TEXT",
    "claim_token": "TEXT",
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
        for column, declaration in _ADDED_COLUMNS.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE tickets ADD COLUMN {column} {declaration}"
                )

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
        the race.
        """

        now = utc_now()
        token = secrets.token_hex(8)
        with self._lock:
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
            query = "SELECT id FROM tickets WHERE status = ?"
            params: list[object] = [TicketStatus.PENDING.value]
            if names is not None:
                query += f" AND worker IN ({', '.join('?' for _ in names)})"
                params.extend(names)
            query += " ORDER BY created_at ASC, rowid ASC LIMIT 1"
            row = self._conn.execute(query, params).fetchone()
            if row is None:
                return None
            ticket_id = row["id"]
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


def _value(value: object) -> object:
    if isinstance(value, TicketStatus):
        return value.value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


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
        log_path=row["log_path"],
        error=row["error"],
        exit_code=row["exit_code"],
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        attempts=int(row["attempts"] or 0),
        claim_token=row["claim_token"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        finished_at=row["finished_at"],
    )
