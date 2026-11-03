"""SQLite persistence for tickets."""

from __future__ import annotations

import json
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
}

# Fields that callers may update through ``set_status``.
UPDATABLE_FIELDS = frozenset(FIELDS) - {"id", "created_at"}


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

    def save(self, ticket: Ticket) -> Ticket:
        """Persist every mutable field of ``ticket``."""

        assignments = ", ".join(f"{name} = ?" for name in FIELDS if name != "id")
        values = tuple(
            _value(getattr(ticket, name)) for name in FIELDS if name != "id"
        ) + (ticket.id,)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE tickets SET {assignments} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(f"ticket not found: {ticket.id}")
            self._conn.commit()
        return ticket

    def set_status(
        self,
        ticket_id: str,
        status: TicketStatus | str,
        *,
        touch: bool = True,
        **fields: object,
    ) -> Ticket:
        """Update a ticket's status (plus optional fields) atomically.

        ``touch`` refreshes ``updated_at``; broadcast-only writes such as a
        heartbeat pass ``touch=False`` so the row's mtime keeps meaning
        "last real state change".
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

            ticket.status = TicketStatus(status)
            for name, value in fields.items():
                setattr(ticket, name, value)
            if touch:
                ticket.updated_at = utc_now()
            return self.save(ticket)

    def heartbeat(self, ticket_id: str) -> Ticket | None:
        """Refresh ``heartbeat_at`` for a running ticket.

        Returns ``None`` when the ticket is gone or no longer running (for
        example because another process reclaimed it).
        """

        with self._lock:
            ticket = self.get(ticket_id)
            if ticket is None or ticket.status is not TicketStatus.RUNNING:
                return None
            return self.set_status(
                ticket_id,
                TicketStatus.RUNNING,
                touch=False,
                heartbeat_at=utc_now(),
            )

    def recover_stale_running(
        self,
        *,
        stale_seconds: float = 300.0,
        reason: str | None = None,
    ) -> list[Ticket]:
        """Fail ``running`` tickets whose heartbeat is older than the threshold.

        The heartbeat is the liveness signal: a row whose ``heartbeat_at`` is
        recent (a live worker in another process) is left alone.  A running
        row that never recorded a heartbeat is stale by definition, which
        also covers rows written by the pre-heartbeat MVP.
        """

        threshold = float(stale_seconds)
        now = datetime.now(timezone.utc)
        recovered: list[Ticket] = []
        for ticket in self.list_tickets(status=TicketStatus.RUNNING, limit=None):
            stamp = _parse_ts(ticket.heartbeat_at)
            if stamp is not None and (now - stamp).total_seconds() < threshold:
                continue
            if stamp is None:
                age = "none recorded"
            else:
                age = f"{int((now - stamp).total_seconds())}s"
            message = reason or (
                "interrupted: heartbeat "
                f"{age} (threshold {threshold:g}s); process likely died"
            )
            recovered_ticket = self.set_status(
                ticket.id,
                TicketStatus.FAILED,
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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        finished_at=row["finished_at"],
    )
