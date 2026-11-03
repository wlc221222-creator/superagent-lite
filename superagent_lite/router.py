"""Router: the single entry point that creates, dispatches and supervises tickets."""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import default_config, normalize_config, resolve_claude_binary
from .logs import append_log
from .models import Ticket, TicketStatus, new_ticket_id, utc_now
from .result import ContractOutcome, check_contract, normalize_artifact
from .store import TicketStore
from .workers import ClaudeCodeWorker, CodexWorker, EchoWorker, Worker, WorkerResult

MAX_SUMMARY_CHARS = 4000
MAX_ARTIFACTS = 1000


class Router:
    """Main agent facade used by the CLI (and later by an HTTP API).

    The router owns the lifecycle of a ticket:

    ``submit`` -> create row (pending) -> ``dispatch`` (running, heartbeat) ->
    worker result -> contract judgement -> done/failed -> persisted result,
    artifacts and log.

    A worker that declares ``result_contract`` is judged by its last ``RESULT``
    line, never by its exit code alone.  A missing or malformed line is a
    failure.  Timeout handling lives in the adapters (the Claude adapter kills
    the process group); the router supplies the timeout and records failures.
    ``retry_count`` / ``max_retries`` are persisted but not yet acted upon.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        store: TicketStore | str | Path | None = None,
        workers: Mapping[str, Worker] | None = None,
        *,
        recover_mode: str = "fail",
    ) -> None:
        self.config = normalize_config(dict(config)) if config is not None else default_config()
        if recover_mode not in ("fail", "requeue", "none"):
            raise ValueError("recover_mode must be 'fail', 'requeue' or 'none'")
        self.recover_mode = recover_mode

        self.runtime_dir = Path(self.config["runtime_dir"]).expanduser().resolve()
        self.work_dir = self.runtime_dir / "work"
        self.log_dir = self.runtime_dir / "logs"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._owns_store = store is None or isinstance(store, (str, Path))
        if store is None:
            store = self.runtime_dir / "superagent.db"
        if isinstance(store, (str, Path)):
            # Recovery is run explicitly below in the router's mode.
            store = TicketStore(
                store,
                stale_seconds=float(self.config["stale_seconds"]),
                recover=False,
            )
        self.store = store
        # A crashed previous process may have left tickets "running".  The
        # synchronous CLI fails them (iteration 2 behaviour); the daemon puts
        # them back on the queue so its pool re-executes them.  The Brain uses
        # "none": it only submits and observes, and must not mutate tickets
        # that a live daemon owns.
        self.recovered: list[Ticket] = []
        if self.recover_mode != "none":
            self.recovered = self.store.recover_stale_running(
                stale_seconds=float(self.config["stale_seconds"]),
                requeue=self.recover_mode == "requeue",
            )
            # Recovery may have failed a ticket that others depend on (retry
            # budget spent).  Those dependents can never run, so fail them now
            # instead of leaving them pending forever.
            for ticket in self.recovered:
                if ticket.status is TicketStatus.FAILED:
                    self.fail_dependents(ticket.id)

        claude_binary = resolve_claude_binary(self.config)
        default_workers: dict[str, Worker] = {
            "echo": EchoWorker(),
            "claude": ClaudeCodeWorker(
                model=self.config.get("claude_model"),
                binary=claude_binary,
                extra_args=self.config.get("claude_extra_args") or [],
                allowed_tools=self.config.get("claude_allowed_tools"),
                max_turns=self.config.get("claude_max_turns", 25),
                permission_mode=self.config.get("claude_permission_mode", "acceptEdits"),
                env_passthrough=self.config.get("claude_env_passthrough"),
                heartbeat_interval=float(self.config.get("heartbeat_seconds", 15.0)),
            ),
            "codex": CodexWorker(),
        }
        if workers:
            default_workers.update(workers)
        self.workers = default_workers

    # -- public API -----------------------------------------------------
    def enqueue(
        self,
        text: str,
        worker: str | Worker | None = None,
        *,
        depends_on: Iterable[str] | None = None,
        workdir: str | Path | None = None,
    ) -> Ticket:
        """Create a ``pending`` ticket and return it immediately.

        The ticket is persisted for a daemon (or a later ``dispatch``) to
        claim; nothing is executed here.  This is what ``superagent queue``
        uses so the submitting side never waits for the run.

        ``depends_on`` lists tickets that must be ``done`` first; the store
        will not hand the ticket out before then.  ``workdir`` overrides the
        default isolated directory, which is how the Brain points every
        ticket of one request at the same user-specified directory.
        """

        description = (text or "").strip()
        if not description:
            raise ValueError("task description must not be empty")

        adapter = self._resolve_worker(worker)
        ticket_id = new_ticket_id()
        dependencies = [str(item) for item in (depends_on or []) if str(item)]
        ticket = Ticket(
            id=ticket_id,
            description=description,
            worker=adapter.name,
            timeout=float(self.config["timeout_seconds"]),
            workdir=str(
                Path(workdir).expanduser().resolve() if workdir is not None
                else self.work_dir / ticket_id
            ),
            log_path=str(self.log_dir / f"{ticket_id}.log"),
            max_retries=int(self.config["max_retries"]),
            depends_on=dependencies,
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
                    "[router] queued (pending); awaiting a daemon",
                ]
            )
            + "\n",
        )
        return ticket

    def submit(self, text: str, worker: str | Worker | None = None) -> Ticket:
        """Create a ticket and dispatch it to a worker synchronously."""

        ticket = self.enqueue(text, worker=worker)
        return self.dispatch(ticket.id, worker=worker)

    def dispatch(self, ticket_id: str, worker: str | Worker | None = None) -> Ticket:
        """Run an existing pending ticket through its worker.

        The pending -> running transition is an atomic claim, so a daemon and
        a synchronous ``submit`` can never execute the same ticket twice.
        Every later write is conditional on the lease token claimed here.
        """

        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")

        adapter = self._resolve_worker(worker if worker is not None else ticket.worker)
        ticket = self.store.claim(ticket_id)
        if ticket is None:
            # Already running, done, or claimed by a daemon.  Return the
            # current row untouched rather than executing it a second time.
            return self.store.get(ticket_id)  # type: ignore[return-value]

        append_log(
            ticket.log_path,
            f"[{ticket.started_at}] started with {adapter.name} "
            f"(attempt {ticket.attempts})\n",
        )
        return self.run_claimed(adapter, ticket)

    def run_claimed(self, adapter: Worker, ticket: Ticket) -> Ticket:
        """Execute a ticket whose lease this process already holds.

        Shared by the synchronous :meth:`dispatch` and the daemon worker pool:
        both pass a ticket that ``TicketStore.claim*`` returned running and
        carrying a ``claim_token``.  The token guards every write, so if the
        lease is lost mid-run (watchdog requeue) the late result is discarded
        instead of clobbering the ticket.
        """

        token = ticket.claim_token
        append_log(
            ticket.log_path,
            f"[{utc_now()}] [router] claimed lease {token} "
            f"(worker={adapter.name})\n",
        )
        started = time.monotonic()
        heartbeat = self._make_heartbeat(ticket, token)
        heartbeat.start()
        try:
            result = self._run_worker(
                adapter, ticket, Path(ticket.workdir), heartbeat
            )
        except KeyboardInterrupt:
            self._fail(ticket.id, "interrupted by user", started, token=token)
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure must be recorded
            self._fail(
                ticket.id, f"{type(exc).__name__}: {exc}", started, token=token
            )
            if isinstance(exc, SystemExit):
                raise
            return self.store.get(ticket.id)  # type: ignore[return-value]
        finally:
            heartbeat.stop()

        duration = time.monotonic() - started
        finished_at = utc_now()
        outcome = self._judge(adapter, result, Path(ticket.workdir))

        if outcome.success:
            updated = self.store.set_status(
                ticket.id,
                TicketStatus.DONE,
                expected_claim_token=token,
                result_summary=_clip(outcome.summary or result.summary, MAX_SUMMARY_CHARS),
                artifacts=outcome.artifacts[:MAX_ARTIFACTS],
                error=None,
                exit_code=result.exit_code,
                finished_at=finished_at,
            )
        else:
            updated = self.store.set_status(
                ticket.id,
                TicketStatus.FAILED,
                expected_claim_token=token,
                result_summary=_clip(outcome.summary or result.summary, MAX_SUMMARY_CHARS) or None,
                artifacts=outcome.artifacts[:MAX_ARTIFACTS],
                error=_clip(outcome.error or "worker reported failure", MAX_SUMMARY_CHARS),
                exit_code=result.exit_code,
                finished_at=finished_at,
            )

        if updated is None:
            append_log(
                ticket.log_path,
                f"[{finished_at}] [router] lease {token} was lost; discarding "
                f"late result (success={outcome.success})\n",
            )
            return self.store.get(ticket.id)  # type: ignore[return-value]

        append_log(
            updated.log_path,
            "\n".join(
                [
                    f"[{finished_at}] finished status={updated.status.value} "
                    f"elapsed={duration:.2f}s",
                    f"[router] artifacts={len(updated.artifacts)}"
                    + (f" {updated.artifacts}" if updated.artifacts else ""),
                ]
            )
            + "\n",
        )
        if updated.status is TicketStatus.FAILED:
            self.fail_dependents(updated.id)
        return updated

    def fail_dependents(self, ticket_id: str) -> list[Ticket]:
        """Cascade a failure to every pending ticket that depends on it.

        A dependency that failed can never be satisfied, so its dependents
        are failed too (with an explicit reason) instead of sitting pending
        forever.  Returns the tickets that were failed, transitively.
        """

        failed: list[Ticket] = []
        frontier = [ticket_id]
        while frontier:
            current = frontier.pop(0)
            for dependent in self.store.fail_dependents(
                current, reason=f"dependency failed: {current}"
            ):
                append_log(
                    dependent.log_path,
                    f"[{utc_now()}] [router] failed because dependency {current} "
                    f"failed\n",
                )
                failed.append(dependent)
                frontier.append(dependent.id)
        return failed

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

    def _make_heartbeat(
        self, ticket: Ticket, token: str | None = None
    ) -> "_Heartbeat":
        """Return a lease-checked heartbeat ticker for ``ticket``."""

        def heartbeat() -> None:
            try:
                if self.store.heartbeat(ticket.id, claim_token=token) is not None:
                    append_log(ticket.log_path, f"[{utc_now()}] [router] heartbeat\n")
            except Exception:  # pragma: no cover - never kill a run over a tick
                pass

        return _Heartbeat(heartbeat, float(self.config["heartbeat_seconds"]))

    def _run_worker(
        self,
        adapter: Worker,
        ticket: Ticket,
        workdir: Path,
        heartbeat: "_Heartbeat",
    ) -> WorkerResult:
        """Call ``adapter.run``, supplying a heartbeat hook when supported."""

        def beat() -> None:
            heartbeat.tick()

        try:
            parameters = inspect.signature(adapter.run).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            parameters = {}
        if "on_heartbeat" in parameters:
            return adapter.run(ticket, workdir, on_heartbeat=beat)
        return adapter.run(ticket, workdir)

    def _judge(
        self, adapter: Worker, result: WorkerResult, workdir: Path
    ) -> ContractOutcome:
        """Decide success from the contract and normalize artifact paths."""

        if adapter.result_contract and result.success:
            # The process ended cleanly; only the RESULT line decides success.
            outcome = check_contract(result.output, result.exit_code)
            if not outcome.success and not outcome.summary:
                outcome.summary = result.summary
        elif adapter.result_contract:
            # Execution-level failure (not found, timeout, non-zero exit,
            # sandbox violation): keep the adapter's precise error.
            outcome = ContractOutcome(
                success=False,
                summary=result.summary,
                error=result.error or "worker failed",
            )
        else:
            # No RESULT line to parse: the adapter's own verdict is
            # authoritative, artifacts included (e.g. custom workers).
            outcome = ContractOutcome(
                success=bool(result.success),
                summary=result.summary,
                artifacts=list(result.artifacts),
                error=None if result.success else (result.error or "worker reported failure"),
            )

        artifacts: list[str] = []
        for raw in outcome.artifacts:
            normalized = normalize_artifact(raw, workdir)
            if normalized and normalized not in artifacts:
                artifacts.append(normalized)
        outcome.artifacts = artifacts
        return outcome

    def _fail(
        self, ticket_id: str, error: str, started: float, *, token: str | None = None
    ) -> None:
        finished_at = utc_now()
        try:
            ticket = self.store.set_status(
                ticket_id,
                TicketStatus.FAILED,
                expected_claim_token=token,
                error=_clip(error, MAX_SUMMARY_CHARS),
                finished_at=finished_at,
            )
            if ticket is None:
                return
            append_log(
                ticket.log_path,
                f"[{finished_at}] failed after {time.monotonic() - started:.2f}s: {error}\n",
            )
            self.fail_dependents(ticket_id)
        except Exception:  # pragma: no cover - preserving the original failure wins
            pass


class _Heartbeat:
    """Background ticker that refreshes a running ticket's heartbeat.

    The router owns this so every worker benefits, including ones that do not
    accept the optional ``on_heartbeat`` callback.
    """

    def __init__(self, callback: Callable[[], None], interval: float) -> None:
        self.callback = callback
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="ticket-heartbeat", daemon=True
        )
        self._thread.start()

    def tick(self) -> None:
        with self._lock:
            self.callback()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # pragma: no cover - a tick must never kill a run
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated, see log)"
