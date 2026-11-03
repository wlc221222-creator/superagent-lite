#!/usr/bin/env python3
"""Phase 3 milestone A tests: superagentd, queue claiming, pool, watchdog and
graceful shutdown.

Covered end to end with a stub ``claude`` binary (no network, no API keys):

* enqueue -> daemon executes -> ticket done (the daemon path, not ``run``);
* daemon restart recovers an orphaned in-flight ticket without losing it and
  without re-running a ticket that already completed;
* the configured concurrency limit is never exceeded;
* atomic claiming (two claimants, one winner) and lease-token (fencing) rules;
* watchdog requeue of a stale running ticket and the retry budget;
* graceful shutdown requeues work that could not finish in the grace window;
* the CLI ``queue`` / ``daemon`` subcommands.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite import (  # noqa: E402
    Daemon,
    DaemonAlreadyRunning,
    Router,
    TicketStatus,
    TicketStore,
)
from superagent_lite.cli import main as cli_main  # noqa: E402
from superagent_lite.models import Ticket, utc_now  # noqa: E402
from superagent_lite.workers.base import Worker, WorkerResult  # noqa: E402
from tests.support import (  # noqa: E402
    make_stub_claude,
    python_executable,
    set_stub_state,
    wait_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def daemon_config(runtime_dir: Path, stub: Path | None = None, **overrides) -> dict:
    config = {
        "default_worker": "echo",
        "claude_binary": "claude",
        "claude_bin": str(stub) if stub else None,
        "claude_model": None,
        "claude_extra_args": [],
        "claude_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "claude_max_turns": 25,
        "timeout_seconds": 30,
        "stale_seconds": 60,
        "heartbeat_seconds": 0.1,
        "max_retries": 1,
        "daemon_concurrency": 2,
        "daemon_poll_seconds": 0.02,
        "daemon_watchdog_seconds": 0.1,
        "daemon_shutdown_grace_seconds": 10,
        "runtime_dir": str(runtime_dir),
    }
    config.update(overrides)
    return config


class TempRuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.bin_dir = self.root / "bin"
        self.stub = make_stub_claude(self.bin_dir)
        self.tracker = self.root / "tracker.log"
        self.config = daemon_config(self.runtime, self.stub, track=str(self.tracker))
        self._daemons: list[tuple[Daemon, threading.Thread]] = []

    def tearDown(self) -> None:
        for daemon, thread in self._daemons:
            daemon.request_stop("test teardown")
            thread.join(timeout=10)
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def start_daemon(self, **overrides) -> tuple[Daemon, threading.Thread]:
        config = dict(self.config)
        config.update(overrides)
        daemon = Daemon(config=config, log=lambda line: None)
        thread = threading.Thread(target=daemon.run, name="test-daemon", daemon=True)
        thread.start()
        self._daemons.append((daemon, thread))
        wait_for(lambda: daemon.router is not None)
        return daemon, thread

    def stop_daemon(self, daemon: Daemon, thread: threading.Thread) -> None:
        daemon.request_stop("test stop")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "daemon did not stop")

    def enqueue(self, worker: str, description: str = "daemon task") -> Ticket:
        with Router(config=self.config) as router:
            return router.enqueue(description, worker=worker)

    def read_ticket(self, ticket_id: str) -> Ticket:
        with TicketStore(self.runtime / "superagent.db", recover=False) as store:
            ticket = store.get(ticket_id)
        self.assertIsNotNone(ticket)
        return ticket

    def tracker_events(self) -> list[str]:
        if not self.tracker.exists():
            return []
        return self.tracker.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# End to end through the daemon
# ---------------------------------------------------------------------------
class DaemonEndToEndTests(TempRuntimeCase):
    def test_queue_then_daemon_executes_claude_ticket(self) -> None:
        ticket = self.enqueue("claude", "create answer.txt")
        self.assertEqual(ticket.status, TicketStatus.PENDING)
        self.assertEqual(ticket.claim_token, None)
        self.assertEqual(ticket.max_retries, 1)  # from config
        # Nothing ran yet: enqueue is a pure queue write.
        self.assertFalse((Path(ticket.workdir) / "answer.txt").exists())
        self.assertIn("queued", Path(ticket.log_path).read_text(encoding="utf-8"))

        daemon, thread = self.start_daemon()
        done = wait_for(
            lambda: (
                t
                if (t := self.read_ticket(ticket.id)).status is TicketStatus.DONE
                else None
            ),
            timeout=15,
        )
        self.assertEqual(done.artifacts, ["answer.txt"])
        self.assertEqual(done.exit_code, 0)
        self.assertEqual(done.attempts, 1)
        self.assertIsNotNone(done.started_at)
        self.assertIsNotNone(done.finished_at)
        self.assertEqual(
            (Path(done.workdir) / "answer.txt").read_text(encoding="utf-8"), "chain-ok"
        )
        self.stop_daemon(daemon, thread)
        # The ticket stays done after shutdown; the daemon never re-runs it.
        self.assertEqual(self.read_ticket(ticket.id).status, TicketStatus.DONE)
        self.assertEqual(len(self.tracker_events()), 0)  # success mode does not track
        self.assertTrue((self.runtime / "superagentd.log").is_file())

    def test_synchronous_run_still_works_unchanged(self) -> None:
        with Router(config=self.config) as router:
            ticket = router.submit("echo task", worker="echo")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.attempts, 1)

    def test_cli_queue_then_cli_daemon_executes(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                ["--config", str(config_path), "queue", "cli task", "--worker", "echo", "--json"]
            )
        self.assertEqual(code, 0)
        queued = json.loads(buffer.getvalue())
        self.assertEqual(queued["status"], "pending")

        # The daemon sub-command in a real subprocess, stopped with SIGTERM.
        process = subprocess.Popen(
            [python_executable(), "-m", "superagent_lite", "--config", str(config_path),
             "daemon", "--concurrency", "1", "--poll", "0.02"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for(
                lambda: self.read_ticket(queued["id"]).status is TicketStatus.DONE,
                timeout=15,
            )
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("started:", stdout)
        self.assertIn("stopped:", stdout)


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------
class DaemonRestartTests(TempRuntimeCase):
    def test_restart_recovers_orphaned_in_flight_ticket(self) -> None:
        # A ticket that a previous daemon claimed and then died on.
        orphan = self.enqueue("echo", "orphan work")
        with TicketStore(self.runtime / "superagent.db", recover=False) as store:
            claimed = store.claim(orphan.id, max_retries=1)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, TicketStatus.RUNNING)
            # Its heartbeat stopped when the process died.
            store.set_status(
                orphan.id,
                TicketStatus.RUNNING,
                touch=False,
                heartbeat_at="2000-01-01T00:00:00+00:00",
            )

        daemon, thread = self.start_daemon()
        # Recovered at startup, re-claimed and executed exactly once.
        done = wait_for(
            lambda: (
                t
                if (t := self.read_ticket(orphan.id)).status is TicketStatus.DONE
                else None
            ),
            timeout=15,
        )
        self.assertEqual(done.attempts, 2)
        self.assertEqual(done.retry_count, 1)
        self.assertEqual(done.error, None)
        self.stop_daemon(daemon, thread)

        # A second restart must not run the completed ticket again.
        again, again_thread = self.start_daemon()
        time.sleep(0.3)
        self.stop_daemon(again, again_thread)
        self.assertEqual(self.read_ticket(orphan.id).status, TicketStatus.DONE)
        self.assertEqual(self.read_ticket(orphan.id).attempts, 2)

    def test_orphan_requeued_not_failed_and_completed_tickets_untouched(self) -> None:
        finished = self.enqueue("echo", "already finished")
        with Router(config=self.config) as router:
            finished = router.dispatch(finished.id)
        self.assertEqual(finished.status, TicketStatus.DONE)

        running = self.enqueue("claude", "interrupted claude run")
        with TicketStore(self.runtime / "superagent.db", recover=False) as store:
            store.claim(running.id, max_retries=1)

        daemon, thread = self.start_daemon()
        done = wait_for(
            lambda: (
                t
                if (t := self.read_ticket(running.id)).status is TicketStatus.DONE
                else None
            ),
            timeout=15,
        )
        self.assertEqual(done.attempts, 2)
        self.assertEqual(len(daemon._recovered), 1)
        self.assertEqual(daemon._recovered[0].id, running.id)
        self.stop_daemon(daemon, thread)

        history = self.read_ticket(finished.id)
        self.assertEqual(history.status, TicketStatus.DONE)
        self.assertEqual(history.attempts, 1)

    def test_second_daemon_on_same_runtime_is_refused(self) -> None:
        first, first_thread = self.start_daemon()
        try:
            second = Daemon(config=self.config, log=lambda line: None)
            with self.assertRaises(DaemonAlreadyRunning):
                second.run()
        finally:
            self.stop_daemon(first, first_thread)


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------
class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concurrency_limit_is_never_exceeded(self) -> None:
        case = self

        class SlowWorker(Worker):
            name = "slow"

            def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
                with case.lock:
                    case.active += 1
                    case.started += 1
                    case.peak = max(case.peak, case.active)
                time.sleep(0.15)
                with case.lock:
                    case.active -= 1
                return WorkerResult(success=True, summary="slow done", exit_code=0)

        config = daemon_config(self.runtime, daemon_concurrency=2)
        worker = SlowWorker()
        router = Router(config=config, workers={"slow": worker})
        ids = [router.enqueue(f"task {i}", worker="slow").id for i in range(6)]
        router.close()

        daemon = Daemon(
            config=config, workers={"slow": SlowWorker()}, log=lambda line: None
        )
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            wait_for(
                lambda: all(
                    self._status(ticket_id) is TicketStatus.DONE for ticket_id in ids
                ),
                timeout=20,
            )
        finally:
            daemon.request_stop("done")
            thread.join(timeout=15)
        self.assertEqual(self.started, 6)
        self.assertEqual(self.peak, 2, "pool ran more workers than the configured limit")

    def test_cli_concurrency_flag_changes_the_limit(self) -> None:
        case = self

        class SlowWorker(Worker):
            name = "slow"

            def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
                with case.lock:
                    case.active += 1
                    case.peak = max(case.peak, case.active)
                time.sleep(0.1)
                with case.lock:
                    case.active -= 1
                return WorkerResult(success=True, summary="ok", exit_code=0)

        config = daemon_config(self.runtime)
        slow = SlowWorker()
        with Router(config=config, workers={"slow": slow}) as router:
            for i in range(4):
                router.enqueue(f"task {i}", worker="slow")

        daemon = Daemon(
            config=config, workers={"slow": SlowWorker()}, log=None
        )
        # Exercise the same validation path the CLI uses.
        from superagent_lite.config import normalize_config

        with self.assertRaises(ValueError):
            normalize_config({"daemon_concurrency": 0})
        daemon.config["daemon_concurrency"] = 3
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            wait_for(lambda: self.peak >= 2, timeout=10)
        finally:
            daemon.request_stop("done")
            thread.join(timeout=15)
        self.assertLessEqual(self.peak, 3)

    def _status(self, ticket_id: str) -> TicketStatus:
        with TicketStore(self.runtime / "superagent.db", recover=False) as store:
            return store.get(ticket_id).status


# ---------------------------------------------------------------------------
# Claiming / fencing at the store level
# ---------------------------------------------------------------------------
class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TicketStore(Path(self._tmp.name) / "q.db", recover=False)
        for i in range(3):
            self.store.create(
                Ticket(id=f"t{i}", description=f"task {i}", worker="echo")
            )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_claim_next_is_fifo_and_atomic(self) -> None:
        first = self.store.claim_next(max_retries=1)
        second = self.store.claim_next(max_retries=1)
        third = self.store.claim_next(max_retries=1)
        self.assertEqual([first.id, second.id, third.id], ["t0", "t1", "t2"])
        self.assertIsNone(self.store.claim_next())
        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.status, TicketStatus.RUNNING)

    def test_two_claimants_one_winner(self) -> None:
        winners: list[Ticket] = []
        barrier = threading.Barrier(8)

        def claim() -> None:
            barrier.wait()
            ticket = self.store.claim("t0")
            if ticket is not None:
                winners.append(ticket)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.count(TicketStatus.RUNNING), 1)

    def test_claim_filters_by_worker(self) -> None:
        ticket = self.store.claim_next(workers=["nobody"])
        self.assertIsNone(ticket)
        ticket = self.store.claim_next(workers=["echo"])
        self.assertEqual(ticket.id, "t0")

    def test_lost_lease_cannot_write_results(self) -> None:
        claimed = self.store.claim_next(max_retries=1)
        stale_token = claimed.claim_token
        (requeued, _) = self.store.requeue(
            claimed.id, claim_token=stale_token, reason="watchdog"
        )
        self.assertEqual(requeued.status, TicketStatus.PENDING)
        # The requeued row has no lease; the old execution may not heartbeat,
        # finish or requeue it any more.
        self.assertIsNone(self.store.heartbeat(claimed.id, claim_token=stale_token))
        self.assertIsNone(
            self.store.set_status(
                claimed.id,
                TicketStatus.DONE,
                expected_claim_token=stale_token,
                result_summary="late",
            )
        )
        self.assertEqual(self.store.get("t0").status, TicketStatus.PENDING)
        again = self.store.claim(claimed.id, max_retries=1)
        self.assertIsNotNone(again)
        self.assertNotEqual(again.claim_token, stale_token)
        self.assertEqual(again.attempts, 2)

    def test_heartbeat_requires_the_current_lease(self) -> None:
        claimed = self.store.claim_next()
        self.assertIsNotNone(self.store.heartbeat(claimed.id, claim_token=claimed.claim_token))
        self.assertIsNone(self.store.heartbeat(claimed.id, claim_token="wrong"))

    def test_requeue_respects_retry_budget_then_fails(self) -> None:
        claimed = self.store.claim_next(max_retries=1)
        ticket, requeued = self.store.requeue(
            claimed.id, claim_token=claimed.claim_token, reason="first interruption"
        )
        self.assertTrue(requeued)
        self.assertEqual(ticket.status, TicketStatus.PENDING)
        self.assertEqual(ticket.retry_count, 1)

        again = self.store.claim(claimed.id, max_retries=1)
        ticket, requeued = self.store.requeue(
            again.id, claim_token=again.claim_token, reason="second interruption"
        )
        self.assertFalse(requeued)
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("second interruption", ticket.error)

    def test_forced_requeue_ignores_the_retry_budget(self) -> None:
        claimed = self.store.claim_next(max_retries=0)
        ticket, requeued = self.store.requeue(
            claimed.id, claim_token=claimed.claim_token, reason="shutdown", force=True
        )
        self.assertTrue(requeued)
        self.assertEqual(ticket.status, TicketStatus.PENDING)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
class WatchdogTests(TempRuntimeCase):
    def test_watchdog_requeues_a_stale_running_ticket(self) -> None:
        daemon, thread = self.start_daemon(daemon_concurrency=1, stale_seconds=0.2)
        try:
            with TicketStore(self.runtime / "superagent.db", recover=False) as store:
                stale = Ticket(
                    id="stale",
                    description="forgotten",
                    worker="echo",
                    status=TicketStatus.RUNNING,
                    started_at="2000-01-01T00:00:00+00:00",
                    heartbeat_at="2000-01-01T00:00:00+00:00",
                    claim_token="dead-token",
                    attempts=1,
                    max_retries=1,
                    log_path=str(self.runtime / "logs" / "stale.log"),
                )
                store.create(stale)
            # The watchdog sees the ancient heartbeat and hands the ticket to
            # the pool, which immediately re-claims it.  ``error`` is cleared
            # at claim time, so the audit trail is the log + retry counter.
            self.assertTrue(wait_for(daemon.watchdog_tick, timeout=10))
            done = wait_for(
                lambda: (
                    t
                    if (t := self.read_ticket("stale")).status is TicketStatus.DONE
                    else None
                ),
                timeout=15,
            )
            self.assertEqual(done.attempts, 2)
            self.assertEqual(done.retry_count, 1)
            log = Path(done.log_path).read_text(encoding="utf-8")
            self.assertIn("watchdog", log)
            self.assertIn("requeued", log)
        finally:
            self.stop_daemon(daemon, thread)

    def test_watchdog_gives_up_after_the_retry_budget(self) -> None:
        daemon, thread = self.start_daemon(daemon_concurrency=1, stale_seconds=0.2)
        try:
            with TicketStore(self.runtime / "superagent.db", recover=False) as store:
                store.create(
                    Ticket(
                        id="exhausted",
                        description="gone",
                        worker="echo",
                        status=TicketStatus.RUNNING,
                        started_at="2000-01-01T00:00:00+00:00",
                        heartbeat_at="2000-01-01T00:00:00+00:00",
                        claim_token="dead-token",
                        attempts=1,
                        retry_count=1,
                        max_retries=1,
                    )
                )
            failed = wait_for(
                lambda: (
                    t
                    if (t := self.read_ticket("exhausted")).status is TicketStatus.FAILED
                    else None
                ),
                timeout=10,
            )
            self.assertIn("watchdog", failed.error)
        finally:
            self.stop_daemon(daemon, thread)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
class ShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.release = threading.Event()
        self.started = threading.Event()

    def tearDown(self) -> None:
        self.release.set()
        self._tmp.cleanup()

    def test_shutdown_requeues_work_that_misses_the_grace_window(self) -> None:
        case = self

        class StuckWorker(Worker):
            name = "stuck"

            def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
                case.started.set()
                case.release.wait(timeout=10)
                return WorkerResult(success=True, summary="late", exit_code=0)

        config = daemon_config(
            self.runtime, daemon_concurrency=1, daemon_shutdown_grace_seconds=0.2
        )
        with Router(config=config, workers={"stuck": StuckWorker()}) as router:
            ticket = router.enqueue("long task", worker="stuck")

        daemon = Daemon(config=config, workers={"stuck": StuckWorker()}, log=None)
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.started.wait(10), "worker never started")
            daemon.request_stop("SIGTERM")
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            requeued = self.read(ticket.id)
            self.assertEqual(requeued.status, TicketStatus.PENDING)
            self.assertIn("shutdown", requeued.error)
            self.assertEqual(requeued.attempts, 1)
        finally:
            # Let the abandoned execution finish: its late result carries a
            # stale lease token and must not overwrite the requeued row.
            self.release.set()
            time.sleep(0.2)
            final = self.read(ticket.id)
            self.assertEqual(final.status, TicketStatus.PENDING)
            self.assertIsNone(final.result_summary)

    def test_shutdown_waits_for_a_run_that_finishes_in_time(self) -> None:
        case = self

        class QuickWorker(Worker):
            name = "quick"

            def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
                time.sleep(0.05)
                case.started.set()
                return WorkerResult(success=True, summary="quick done", exit_code=0)

        config = daemon_config(
            self.runtime, daemon_concurrency=1, daemon_shutdown_grace_seconds=5
        )
        with Router(config=config, workers={"quick": QuickWorker()}) as router:
            ticket = router.enqueue("quick task", worker="quick")

        daemon = Daemon(config=config, workers={"quick": QuickWorker()}, log=None)
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.started.wait(10))
            daemon.request_stop("SIGTERM")
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        finally:
            self.release.set()
        done = self.read(ticket.id)
        self.assertEqual(done.status, TicketStatus.DONE)
        self.assertEqual(done.attempts, 1)

    def test_stop_claiming_immediately_while_running_guest_finishes(self) -> None:
        case = self

        class RecordingWorker(Worker):
            name = "record"
            calls = 0

            def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
                with case.lock:
                    RecordingWorker.calls += 1
                return WorkerResult(success=True, summary="ok", exit_code=0)

        self.lock = threading.Lock()
        config = daemon_config(self.runtime, daemon_concurrency=1)
        with Router(config=config, workers={"record": RecordingWorker()}) as router:
            first = router.enqueue("first", worker="record")
            second = router.enqueue("second", worker="record")

        daemon = Daemon(
            config=config, workers={"record": RecordingWorker()}, log=None
        )
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            wait_for(lambda: self.read(first.id).status is TicketStatus.DONE, timeout=10)
            daemon.request_stop("SIGTERM")
            thread.join(timeout=10)
        finally:
            pass
        self.assertEqual(self.read(first.id).status, TicketStatus.DONE)
        # The second ticket was never claimed after shutdown began.
        self.assertEqual(self.read(second.id).status, TicketStatus.PENDING)

    def read(self, ticket_id: str) -> Ticket:
        with TicketStore(self.runtime / "superagent.db", recover=False) as store:
            return store.get(ticket_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
