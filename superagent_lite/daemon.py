"""superagentd: the resident queue daemon.

The daemon owns a :class:`~superagent_lite.router.Router` (and therefore the
same result contract, sandboxing and heartbeat machinery as the synchronous
CLI) and runs a small worker pool over the SQLite queue:

* **Claiming** -- the oldest ``pending`` ticket is taken with a single
  conditional ``UPDATE`` (``WHERE status='pending'``) and gets a fresh lease
  token.  Two processes racing for the same row cannot both win.
* **Pool** -- at most ``daemon_concurrency`` tickets execute at once; the pool
  is plain daemon threads so a forced shutdown is never blocked by Python's
  interpreter-exit join.
* **Heartbeats** -- the router refreshes ``heartbeat_at`` every
  ``heartbeat_seconds`` while a worker runs, using the lease token.
* **Watchdog** -- every ``daemon_watchdog_seconds`` a running ticket whose
  heartbeat is older than ``stale_seconds`` is requeued (or failed once its
  retry budget is spent).
* **Restart recovery** -- a per-runtime-dir exclusive lock guarantees at most
  one daemon.  Since no live daemon can exist while we hold it, every
  ``running`` ticket found at startup is an orphan and is put back on the
  queue before the pool starts.
* **Graceful shutdown** -- on SIGTERM/SIGINT the pool stops claiming, gives
  in-flight runs ``daemon_shutdown_grace_seconds`` to finish, then requeues
  whatever is still running.  Late results from a cancelled run carry a stale
  lease token and are discarded instead of overwriting the requeued row.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import default_config, normalize_config
from .logs import append_log
from .models import Ticket, TicketStatus, utc_now
from .router import Router
from .store import TicketStore
from .workers import Worker

try:  # POSIX only; the daemon is a server-side component.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]


class DaemonAlreadyRunning(RuntimeError):
    """Raised when another daemon already owns this runtime directory."""


class _DaemonLock:
    """Exclusive, non-blocking advisory lock for one runtime directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except (BlockingIOError, OSError):
            handle.close()
            raise DaemonAlreadyRunning(
                f"another superagentd is already running for {self.path.parent} "
                f"(lock: {self.path})"
            ) from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - closing releases the lock anyway
            pass
        self._handle.close()
        self._handle = None


class Daemon:
    """Persistent queue + worker pool + watchdog."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        workers: Mapping[str, Worker] | None = None,
        store: TicketStore | str | Path | None = None,
        log: Callable[[str], None] | None = print,
        concurrency: int | None = None,
        poll_seconds: float | None = None,
        watchdog_seconds: float | None = None,
        max_retries: int | None = None,
        recover: bool = True,
    ) -> None:
        values = dict(config) if config is not None else {}
        overrides = {
            "daemon_concurrency": concurrency,
            "daemon_poll_seconds": poll_seconds,
            "daemon_watchdog_seconds": watchdog_seconds,
            "max_retries": max_retries,
        }
        for key, value in overrides.items():
            if value is not None:
                values[key] = value
        self.config = normalize_config(values) if values else default_config()

        self._workers = dict(workers) if workers is not None else None
        self._store = store
        self._log_fn = log
        self._recover = recover

        self.router: Router | None = None
        self.log_path: Path | None = None
        self._lock: _DaemonLock | None = None

        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._active: dict[str, str | None] = {}
        self._threads: set[threading.Thread] = set()
        self._watchdog_thread: threading.Thread | None = None
        self._reason = "stopped"
        self._old_handlers: dict[int, Any] = {}
        self._recovered: list[Ticket] = []

    # -- lifecycle ------------------------------------------------------
    def run(self) -> int:
        """Run the daemon until SIGTERM/SIGINT or :meth:`request_stop`."""

        runtime_dir = Path(self.config["runtime_dir"]).expanduser().resolve()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = runtime_dir / "superagentd.log"

        self._lock = _DaemonLock(runtime_dir / "superagentd.lock")
        self._lock.acquire()
        self._install_signal_handlers()
        try:
            self.router = Router(
                config=self.config,
                store=self._store,
                workers=self._workers,
                recover_mode="requeue",
            )
            if self._recover:
                self._recover_orphans()
            self._log(
                "started: concurrency={concurrency} poll={poll:g}s watchdog={watchdog:g}s "
                "stale={stale:g}s max_retries={retries} pid={pid}".format(
                    concurrency=self.config["daemon_concurrency"],
                    poll=float(self.config["daemon_poll_seconds"]),
                    watchdog=float(self.config["daemon_watchdog_seconds"]),
                    stale=float(self.config["stale_seconds"]),
                    retries=self.config["max_retries"],
                    pid=os.getpid(),
                )
            )
            self._start_watchdog()
            self._loop()
        finally:
            self._shutdown()
            self._restore_signal_handlers()
            if self.router is not None:
                self.router.close()
            assert self._lock is not None
            self._lock.release()
            self._lock = None
        return 0

    def request_stop(self, reason: str = "stop requested") -> None:
        """Ask the daemon to stop; safe to call from a signal or a thread."""

        if not self._stop.is_set():
            self._reason = reason
            self._stop.set()

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._active)

    # -- main loop ------------------------------------------------------
    def _loop(self) -> None:
        assert self.router is not None
        poll = float(self.config["daemon_poll_seconds"])
        worker_names = sorted(self.router.workers)
        while not self._stop.is_set():
            while not self._stop.is_set() and self._try_spawn(worker_names):
                pass
            self._stop.wait(poll)
        self._stop.set()

    def _try_spawn(self, worker_names: list[str]) -> bool:
        """Claim and start one ticket, or return ``False`` when idle/full."""

        assert self.router is not None
        store = self.router.store
        concurrency = int(self.config["daemon_concurrency"])
        with self._condition:
            if len(self._active) >= concurrency:
                return False
            ticket = store.claim_next(
                workers=worker_names,
                max_retries=int(self.config["max_retries"]),
            )
            if ticket is None:
                return False
            adapter = self.router.workers.get(ticket.worker)
            if adapter is None:  # pragma: no cover - claim filtered by name
                store.set_status(
                    ticket.id,
                    TicketStatus.FAILED,
                    expected_claim_token=ticket.claim_token,
                    error=f"daemon has no worker adapter named {ticket.worker!r}",
                    finished_at=utc_now(),
                )
                return True
            self._prepare(adapter, ticket)
            self._active[ticket.id] = ticket.claim_token
            thread = threading.Thread(
                target=self._execute,
                args=(adapter, ticket),
                name=f"superagentd-{ticket.id}",
                daemon=True,
            )
            self._threads.add(thread)
            thread.start()
        self._log(
            f"claimed {ticket.id} worker={ticket.worker} attempt={ticket.attempts} "
            f"active={self.active_count}/{concurrency}"
        )
        return True

    def _prepare(self, adapter: Worker, ticket: Ticket) -> None:
        """Make sure workdir/log exist for tickets created outside ``enqueue``."""

        assert self.router is not None
        workdir = ticket.workdir or str(self.router.work_dir / ticket.id)
        log_path = ticket.log_path or str(self.router.log_dir / f"{ticket.id}.log")
        Path(workdir).mkdir(parents=True, exist_ok=True)
        changed = (ticket.workdir, ticket.log_path) != (workdir, log_path)
        ticket.workdir = workdir
        ticket.log_path = log_path
        if changed:
            self.router.store.set_status(
                ticket.id,
                TicketStatus.RUNNING,
                expected_claim_token=ticket.claim_token,
                workdir=workdir,
                log_path=log_path,
            )

    def _execute(self, adapter: Worker, ticket: Ticket) -> None:
        assert self.router is not None
        status = "failed"
        try:
            updated = self.router.run_claimed(adapter, ticket)
            status = updated.status.value if updated is not None else "lost-lease"
        except BaseException as exc:  # noqa: BLE001 - keep the pool alive
            try:
                self.router.store.set_status(
                    ticket.id,
                    TicketStatus.FAILED,
                    expected_claim_token=ticket.claim_token,
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=utc_now(),
                )
            except Exception:  # pragma: no cover - store is the last resort
                pass
            status = f"crashed ({type(exc).__name__}: {exc})"
            self._cascade_failures(ticket.id)
        finally:
            with self._condition:
                self._active.pop(ticket.id, None)
                self._threads.discard(threading.current_thread())
                self._condition.notify_all()
        self._log(f"finished {ticket.id}: {status}")

    # -- watchdog / recovery -------------------------------------------
    def _start_watchdog(self) -> None:
        interval = float(self.config["daemon_watchdog_seconds"])
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, args=(interval,), name="superagentd-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            self.watchdog_tick()

    def watchdog_tick(self) -> list[Ticket]:
        """Requeue running tickets whose heartbeat went stale."""

        if self.router is None:
            return []
        try:
            recovered = self.router.store.recover_stale_running(
                stale_seconds=float(self.config["stale_seconds"]),
                requeue=True,
                reason=(
                    "watchdog: heartbeat older than "
                    f"{float(self.config['stale_seconds']):g}s"
                ),
            )
        except Exception as exc:  # pragma: no cover - never kill the daemon
            self._log(f"watchdog error: {type(exc).__name__}: {exc}")
            return []
        for ticket in recovered:
            self._log(
                f"watchdog recovered {ticket.id}: status={ticket.status.value} "
                f"retry={ticket.retry_count}/{ticket.max_retries}"
            )
            if ticket.status is TicketStatus.FAILED:
                self._cascade_failures(ticket.id)
        return recovered

    def _cascade_failures(self, ticket_id: str) -> None:
        """Fail dependents of a ticket the pool/watchdog gave up on.

        ``Router.run_claimed`` already does this for ordinary worker
        failures; this covers the paths that write ``failed`` directly (a
        crashed pool thread, or a watchdog requeue that exhausted the retry
        budget).  Without it, dependents would sit ``pending`` forever.
        """

        if self.router is None:
            return
        try:
            failed = self.router.fail_dependents(ticket_id)
        except Exception as exc:  # pragma: no cover - never kill the pool
            self._log(f"dependency cascade failed for {ticket_id}: {exc}")
            return
        for ticket in failed:
            self._log(f"failed dependent {ticket.id} of {ticket_id}")

    def _recover_orphans(self) -> None:
        """Requeue everything left ``running`` by a previous daemon.

        The runtime lock guarantees no other daemon is alive, so any running
        row belongs to a process that died without cleaning up.
        """

        assert self.router is not None
        self._recovered = self.router.store.recover_stale_running(
            stale_seconds=0,
            requeue=True,
            reason="orphaned: previous daemon stopped while this ticket was running",
        )
        for ticket in self._recovered:
            self._log(
                f"recovered orphan {ticket.id}: status={ticket.status.value} "
                f"retry={ticket.retry_count}/{ticket.max_retries}"
            )
            if ticket.status is TicketStatus.FAILED:
                self._cascade_failures(ticket.id)

    # -- shutdown -------------------------------------------------------
    def _shutdown(self) -> None:
        if self.router is None:
            return
        store = self.router.store
        grace = float(self.config["daemon_shutdown_grace_seconds"])
        if self._stop.is_set():
            self._log(f"stopping: {self._reason}; no new claims")
        if grace > 0 and self._active:
            self._log(
                f"waiting up to {grace:g}s for {len(self._active)} in-flight ticket(s)"
            )
        deadline = time.monotonic() + grace
        with self._condition:
            while self._active and time.monotonic() < deadline:
                self._condition.wait(min(0.1, max(0.0, deadline - time.monotonic())))
            leftover = dict(self._active)
        for ticket_id, token in leftover.items():
            try:
                ticket, requeued = store.requeue(
                    ticket_id,
                    claim_token=token,
                    reason=f"shutdown: {self._reason} while running; requeued",
                    force=True,
                )
            except Exception as exc:  # pragma: no cover - best effort
                self._log(f"shutdown requeue failed for {ticket_id}: {exc}")
                continue
            if ticket is None:
                continue
            outcome = "requeued (pending)" if requeued else f"left {ticket.status.value}"
            self._log(f"shutdown: {ticket_id} still running -> {outcome}")
        if leftover:
            with self._condition:
                # The pool threads are daemon threads; a late result carries a
                # stale lease token and is discarded by the store.
                self._active.clear()
                self._condition.notify_all()
        self._log(
            f"stopped: active={len(leftover)} "
            f"queue={store.count(TicketStatus.PENDING)} "
            f"done={store.count(TicketStatus.DONE)} "
            f"failed={store.count(TicketStatus.FAILED)}"
        )
        self._stop.set()

    # -- signals / logging ---------------------------------------------
    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not main thread
                pass
        self._old_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - exotic platforms
            name = str(signum)
        self.request_stop(f"received {name}")

    def _log(self, message: str) -> None:
        line = f"[{utc_now()}] [daemon] {message}"
        append_log(self.log_path, line + "\n")
        if self._log_fn is not None:
            try:
                self._log_fn(line)
            except Exception:  # pragma: no cover - a bad sink must not stop us
                pass
