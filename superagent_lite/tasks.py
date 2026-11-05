"""A user's ongoing task, distinct from a single queue execution ticket.

The HTTP Brain owns task progression; the existing daemon owns execution.
Task context survives a chat timeout and is reused for follow-up work.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field

from .models import new_ticket_id, utc_now


@dataclass
class Task:
    request: str
    user_id: str
    id: str = field(default_factory=lambda: "task_" + new_ticket_id()[2:])
    context: list[dict[str, str]] = field(default_factory=list)
    memory: str = ""
    workdir: str = ""
    worker: str = ""
    ticket_ids: list[str] = field(default_factory=list)
    active_ids: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    pending_inputs: list[str] = field(default_factory=list)
    status: str = "working"
    reply: str = ""
    rounds: int = 0
    assignment: int = 0
    pending_plan: dict | None = None
    history_recorded: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def settled(self) -> bool:
        return self.status in ("ready", "needs_input", "failed")


class TaskStore:
    """Task records share the queue database, without changing ticket states."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, data TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, task: Task) -> None:
        task.updated_at = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, "
                "status=excluded.status, updated_at=excluded.updated_at, data=excluded.data",
                (task.id, task.user_id, task.status, task.updated_at,
                 json.dumps(asdict(task), ensure_ascii=False)),
            )

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return Task(**json.loads(row[0])) if row else None

    def list(self, *, user_id: str | None = None, active: bool = False,
             limit: int = 20) -> list[Task]:
        clauses, values = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            values.append(user_id)
        if active:
            clauses.append("status IN ('planning', 'working', 'awaiting_approval')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM tasks" + where + " ORDER BY updated_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [Task(**json.loads(row[0])) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def task_context(task: Task) -> str:
    """Preserve the original words; a router's paraphrase is not the mandate."""
    return json.dumps({
        "original_request": task.request,
        "conversation_at_start": task.context,
        "project_memory_at_start": task.memory,
        "user_followups": task.updates,
    }, ensure_ascii=False, indent=2)
