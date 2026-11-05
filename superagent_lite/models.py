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
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


#: ``review_status`` values.  ``none`` covers unreviewed/legacy tickets.
REVIEW_NONE = "none"
REVIEW_PASSED = "passed"
REVIEW_REJECTED = "rejected"
REVIEW_REJECTED_ONCE = "rejected_once"
REVIEW_INCONCLUSIVE = "inconclusive"


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
    abandoned by a dead process.  ``retry_count`` counts interruptions that
    were put back on the queue; ``max_retries`` is how many of those a ticket
    may suffer before it is failed instead.

    ``claim_token`` identifies the current lease: a daemon sets a fresh token
    when it atomically claims the ticket.  Results and heartbeats from an
    execution are only accepted while the token still matches, so a stale
    worker thread can never overwrite a ticket that was requeued and claimed
    again.
    """

    id: str
    description: str
    worker: str
    status: TicketStatus = TicketStatus.PENDING
    workdir: str = ""
    timeout: float = 300.0
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list)
    # Ticket ids that must reach ``done`` before this ticket may be claimed.
    # The planner (Brain) sets these when it decomposes a request; the store
    # refuses to hand out a ticket whose dependencies are not satisfied.
    depends_on: list[str] = field(default_factory=list)
    log_path: str | None = None
    error: str | None = None
    exit_code: int | None = None
    attempts: int = 0
    retry_count: int = 0
    max_retries: int = 0
    claim_token: str | None = None
    # Phase 5 fields.  Every one has a default so the constructions written
    # for the earlier phases keep working unchanged, and so a row migrated
    # from an old database carries explicit (never NULL) gate inputs.
    origin: str = "cli"
    review_status: str = REVIEW_NONE
    review_reasons: list[str] = field(default_factory=list)
    # Semantic-review rejections, counted separately from execution attempts.
    review_attempts: int = 0
    # sha256(description) captured when the approval gate parked the ticket.
    approval_hash: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
    # One user task can have several execution tickets over its lifetime.
    task_id: str | None = None
    # Populated at dispatch, not stored on the ticket or used by the gate.
    context: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (TicketStatus.DONE, TicketStatus.FAILED)

    @property
    def awaits_approval(self) -> bool:
        return self.status is TicketStatus.AWAITING_APPROVAL
