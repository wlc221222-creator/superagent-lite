"""SQLite persistence for tickets."""

from __future__ import annotations

import sqlite3
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
    log_path       TEXT,
    error          TEXT,
    exit_code      INTEGER,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    max_retries    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    started_at     TEXT,
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
    "log_path",
    "error",
    "exit_code",
    "retry_count",
    "max_retries",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)

# Fields that callers may update through ``set_status``.
UPDATABLE_FIELDS = frozenset(FIELDS) - {"id", "created_at"}


class TicketStore:
    """Small SQLite-backed ticket store.

    One connection is kept per instance; the CLI is single-process and
    single-threaded, which suits the MVP.  Writes are committed immediately so
    a process restart never loses a completed ticket.
    """

    def __init__(self, path: str | Path = "runtime/superagent.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._closed = False

    # -- writes ---------------------------------------------------------
    def create(self, ticket: Ticket) -> Ticket:
        placeholders = ", ".join("?" for _ in FIELDS)
        columns = ", ".join(FIELDS)
        values = tuple(_value(getattr(ticket, name)) for name in FIELDS)
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
        cursor = self._conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?", values
        )
        if cursor.rowcount == 0:
            raise KeyError(f"ticket not found: {ticket.id}")
        self._conn.commit()
        return ticket

    def set_status(self, ticket_id: str, status: TicketStatus | str, **fields: object) -> Ticket:
        """Update a ticket's status (plus optional fields) atomically."""

        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"cannot update unknown ticket fields: {sorted(unknown)}")

        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")

        ticket.status = TicketStatus(status)
        for name, value in fields.items():
            setattr(ticket, name, value)
        ticket.updated_at = utc_now()
        return self.save(ticket)

    def recover_stale_running(
        self, reason: str = "interrupted: process restarted before the ticket finished"
    ) -> list[Ticket]:
        """Mark tickets left ``running`` by a dead process as failed.

        The MVP CLI is single-process, so any ``running`` row at startup is
        stale.  (A future daemon with leases/heartbeats replaces this.)
        """

        recovered: list[Ticket] = []
        for ticket in self.list_tickets(status=TicketStatus.RUNNING, limit=None):
            recovered.append(
                self.set_status(
                    ticket.id,
                    TicketStatus.FAILED,
                    error=reason,
                    finished_at=utc_now(),
                )
            )
        return recovered

    # -- reads ----------------------------------------------------------
    def get(self, ticket_id: str) -> Ticket | None:
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
        rows: Iterable[sqlite3.Row] = self._conn.execute(query, params).fetchall()
        return [_from_row(row) for row in rows]

    def count(self, status: TicketStatus | str | None = None) -> int:
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
    return value


def _from_row(row: sqlite3.Row) -> Ticket:
    return Ticket(
        id=row["id"],
        description=row["description"],
        status=TicketStatus(row["status"]),
        worker=row["worker"],
        workdir=row["workdir"],
        timeout=float(row["timeout"]),
        result_summary=row["result_summary"],
        log_path=row["log_path"],
        error=row["error"],
        exit_code=row["exit_code"],
        retry_count=int(row["retry_count"]),
        max_retries=int(row["max_retries"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
