"""Router: the single entry point that creates, dispatches and supervises tickets."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from .config import default_config, normalize_config
from .models import Ticket, TicketStatus, new_ticket_id, utc_now
from .store import TicketStore
from .workers import ClaudeCodeWorker, CodexWorker, EchoWorker, Worker
from .workers.base import append_log

MAX_SUMMARY_CHARS = 4000


class Router:
    """Main agent facade used by the CLI (and later by an HTTP API).

    The router owns the lifecycle of a ticket:

    ``submit`` -> create row (pending) -> ``dispatch`` (running) ->
    worker result -> done/failed -> persisted result and log.

    Timeout handling lives in the adapters (the Claude adapter kills the
    process group); the router supplies the timeout and records failures.
    ``retry_count`` / ``max_retries`` are persisted but not yet acted upon.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        store: TicketStore | str | Path | None = None,
        workers: Mapping[str, Worker] | None = None,
    ) -> None:
        self.config = normalize_config(dict(config)) if config is not None else default_config()

        self.runtime_dir = Path(self.config["runtime_dir"]).expanduser().resolve()
        self.work_dir = self.runtime_dir / "work"
        self.log_dir = self.runtime_dir / "logs"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._owns_store = store is None or isinstance(store, (str, Path))
        if store is None:
            store = self.runtime_dir / "superagent.db"
        if isinstance(store, (str, Path)):
            store = TicketStore(store)
        self.store = store
        # A crashed previous process may have left tickets "running".
        self.store.recover_stale_running()

        default_workers: dict[str, Worker] = {
            "echo": EchoWorker(),
            "claude": ClaudeCodeWorker(
                model=self.config.get("claude_model"),
                binary=self.config["claude_binary"],
                extra_args=self.config.get("claude_extra_args") or [],
            ),
            "codex": CodexWorker(),
        }
        if workers:
            default_workers.update(workers)
        self.workers = default_workers

    # -- public API -----------------------------------------------------
    def submit(self, text: str, worker: str | Worker | None = None) -> Ticket:
        """Create a ticket and dispatch it to a worker synchronously."""

        description = (text or "").strip()
        if not description:
            raise ValueError("task description must not be empty")

        adapter = self._resolve_worker(worker)
        ticket_id = new_ticket_id()
        ticket = Ticket(
            id=ticket_id,
            description=description,
            worker=adapter.name,
            timeout=float(self.config["timeout_seconds"]),
            workdir=str(self.work_dir / ticket_id),
            log_path=str(self.log_dir / f"{ticket_id}.log"),
        )

        Path(ticket.workdir).mkdir(parents=True, exist_ok=True)
        self.store.create(ticket)
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    f"[{ticket.created_at}] ticket created: {ticket.id}",
                    f"task: {ticket.description}",
                    f"worker: {ticket.worker}",
                    f"workdir: {ticket.workdir}",
                    f"timeout: {ticket.timeout:g}s",
                ]
            )
            + "\n",
        )
        return self.dispatch(ticket.id, worker=adapter)

    def dispatch(self, ticket_id: str, worker: str | Worker | None = None) -> Ticket:
        """Run an existing (typically pending) ticket through its worker."""

        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")

        adapter = self._resolve_worker(worker if worker is not None else ticket.worker)
        ticket = self.store.set_status(
            ticket.id,
            TicketStatus.RUNNING,
            started_at=utc_now(),
            worker=adapter.name,
            error=None,
            exit_code=None,
        )
        append_log(ticket.log_path, f"[{ticket.started_at}] started with {adapter.name}\n")

        started = time.monotonic()
        try:
            result = adapter.run(ticket, Path(ticket.workdir))
        except KeyboardInterrupt:
            self._fail(ticket.id, "interrupted by user", started)
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure must be recorded
            self._fail(ticket.id, f"{type(exc).__name__}: {exc}", started)
            if isinstance(exc, SystemExit):
                raise
            return self.store.get(ticket.id)  # type: ignore[return-value]

        duration = time.monotonic() - started
        finished_at = utc_now()

        if result.success:
            ticket = self.store.set_status(
                ticket.id,
                TicketStatus.DONE,
                result_summary=_clip(result.summary, MAX_SUMMARY_CHARS),
                error=None,
                exit_code=result.exit_code,
                finished_at=finished_at,
            )
        else:
            summary = _clip(result.summary or "", MAX_SUMMARY_CHARS)
            ticket = self.store.set_status(
                ticket.id,
                TicketStatus.FAILED,
                result_summary=summary or None,
                error=_clip(result.error or "worker reported failure", MAX_SUMMARY_CHARS),
                exit_code=result.exit_code,
                finished_at=finished_at,
            )

        append_log(
            ticket.log_path,
            f"[{finished_at}] finished status={ticket.status.value} "
            f"elapsed={duration:.2f}s\n",
        )
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        """Return a ticket, or ``None`` when the id is unknown."""

        return self.store.get(ticket_id)

    def result(self, ticket_id: str) -> Ticket | None:
        """Alias for :meth:`get`, kept for the spec's ``result`` wording."""

        return self.store.get(ticket_id)

    def list(
        self,
        status: TicketStatus | str | None = None,
        limit: int | None = 50,
    ) -> list[Ticket]:
        return self.store.list_tickets(status=status, limit=limit)

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> Router:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- internals ------------------------------------------------------
    def _resolve_worker(self, worker: str | Worker | None) -> Worker:
        if isinstance(worker, Worker):
            return worker
        name = worker or self.config["default_worker"]
        adapter = self.workers.get(name)
        if adapter is None:
            known = ", ".join(sorted(self.workers))
            raise ValueError(f"unknown worker {name!r}; available workers: {known}")
        return adapter

    def _fail(self, ticket_id: str, error: str, started: float) -> None:
        finished_at = utc_now()
        try:
            ticket = self.store.set_status(
                ticket_id,
                TicketStatus.FAILED,
                error=_clip(error, MAX_SUMMARY_CHARS),
                finished_at=finished_at,
            )
            append_log(
                ticket.log_path,
                f"[{finished_at}] failed after {time.monotonic() - started:.2f}s: {error}\n",
            )
        except Exception:  # pragma: no cover - preserving the original failure wins
            pass


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated, see log)"
