"""Data models for superagent-lite."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TicketStatus(str, Enum):
    """Lifecycle states of a ticket."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


def utc_now() -> str:
    """Return the current UTC time as a sortable ISO-8601 string.

    Millisecond precision keeps heartbeats distinguishable even when a run
    is shorter than a second.
    """

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_ticket_id() -> str:
    """Create a short, time-sortable, collision-resistant ticket id."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"t_{stamp}_{secrets.token_hex(3)}"


@dataclass(slots=True)
class Ticket:
    """A unit of work dispatched to exactly one worker.

    ``attempts`` counts dispatches (1 for the first run), ``started_at`` is
    set when a dispatch begins and ``heartbeat_at`` is refreshed while the
    worker runs so that a restarted process can tell a live ticket from one
    abandoned by a dead process.  ``retry_count`` / ``max_retries`` are
    reserved for automatic retries.
    """

    id: str
    description: str
    worker: str
    status: TicketStatus = TicketStatus.PENDING
    workdir: str = ""
    timeout: float = 300.0
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list)
    log_path: str | None = None
    error: str | None = None
    exit_code: int | None = None
    attempts: int = 0
    retry_count: int = 0
    max_retries: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (TicketStatus.DONE, TicketStatus.FAILED)
