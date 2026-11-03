#!/usr/bin/env python3
"""Smoke test: create -> execute -> complete -> query.

Runs with either ``python tests/test_smoke.py`` (no dependencies, discovers
every ``test_*`` function in this module) or ``python -m pytest``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running this file directly from a checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite import (  # noqa: E402
    ClaudeCodeWorker,
    CodexWorker,
    EchoWorker,
    Router,
    TicketStatus,
    TicketStore,
    load_config,
)


def _config(runtime_dir: Path) -> dict:
    return {
        "default_worker": "echo",
        "claude_binary": "claude",
        "claude_model": "claude-sonnet-5-5",
        "claude_extra_args": [],
        "timeout_seconds": 5,
        "runtime_dir": str(runtime_dir),
    }


class SmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name) / "runtime"
        self.config = _config(self.runtime)
        self.router = Router(config=self.config)

    def tearDown(self) -> None:
        self.router.close()
        self._tmp.cleanup()

    # -- the acceptance path -------------------------------------------
    def test_echo_ticket_lifecycle(self) -> None:
        task = "写一个测试任务"
        ticket = self.router.submit(task, worker="echo")

        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.worker, "echo")
        self.assertIn(task, ticket.result_summary)
        self.assertIsNotNone(ticket.started_at)
        self.assertIsNotNone(ticket.finished_at)

        # Isolated working directory and audit log exist on disk.
        workdir = Path(ticket.workdir)
        self.assertTrue(workdir.is_dir())
        self.assertEqual(workdir.parent, self.runtime / "work")
        self.assertTrue((self.runtime / "superagent.db").is_file())
        self.assertTrue(Path(ticket.log_path).is_file())
        log = Path(ticket.log_path).read_text(encoding="utf-8")
        self.assertIn(task, log)
        self.assertIn("finished status=done", log)

        # Query paths exposed by the router.
        self.assertEqual(self.router.result(ticket.id).id, ticket.id)
        listed = self.router.list()
        self.assertEqual([t.id for t in listed], [ticket.id])
        self.assertEqual(self.router.list(status="done")[0].id, ticket.id)

    def test_ticket_survives_router_restart(self) -> None:
        ticket = self.router.submit("persist me", worker=EchoWorker())
        self.router.close()

        reopened = Router(config=self.config)
        try:
            loaded = reopened.get(ticket.id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, TicketStatus.DONE)
            self.assertEqual(loaded.result_summary, "echo: persist me")
        finally:
            reopened.close()

    def test_stale_running_ticket_is_recovered_on_restart(self) -> None:
        ticket = self.router.submit("will look interrupted", worker="echo")
        # Simulate a process that died while running: the row stays running
        # and its heartbeat stops well before the stale threshold.
        self.router.store.set_status(
            ticket.id,
            "running",
            error=None,
            heartbeat_at="2000-01-01T00:00:00+00:00",
        )
        self.router.close()

        reopened = Router(config=self.config)
        try:
            loaded = reopened.get(ticket.id)
            self.assertEqual(loaded.status, TicketStatus.FAILED)
            self.assertIn("interrupted", loaded.error)
            self.assertIn("heartbeat", loaded.error)
        finally:
            reopened.close()

    def test_default_worker_from_config(self) -> None:
        ticket = self.router.submit("use the configured default")
        self.assertEqual(ticket.worker, "echo")
        self.assertEqual(ticket.status, TicketStatus.DONE)

    # -- failure handling ----------------------------------------------
    def test_codex_placeholder_records_failure(self) -> None:
        ticket = self.router.submit("codex task", worker="codex")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("NotImplementedError", ticket.error)
        self.assertIn("placeholder", ticket.error)

    def test_missing_claude_binary_records_failure(self) -> None:
        router = Router(
            config=self.config,
            workers={"claude": ClaudeCodeWorker(binary="superagent-no-such-claude")},
        )
        try:
            ticket = router.submit("claude task", worker="claude")
        finally:
            router.close()
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("not found", ticket.error)

    def test_empty_task_and_unknown_worker_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.router.submit("   ", worker="echo")
        with self.assertRaises(ValueError):
            self.router.submit("task", worker="nope")

    def test_codex_worker_raises_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            CodexWorker().run(None, Path("."))  # type: ignore[arg-type]

    # -- store ----------------------------------------------------------
    def test_store_status_transitions(self) -> None:
        with TicketStore(self.runtime / "store.db") as store:
            ticket = self.router.submit("store check", worker="echo")
            store.create(ticket)
            store.set_status(ticket.id, "running")
            updated = store.set_status(ticket.id, "done", result_summary="ok")
            self.assertEqual(updated.status, TicketStatus.DONE)
            self.assertEqual(store.get(ticket.id).result_summary, "ok")
            self.assertEqual(store.count(), 1)
            self.assertIsNone(store.get("missing"))

    def test_invalid_config_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Router(config={"timeout_seconds": -1})

    # -- config ---------------------------------------------------------
    def test_load_config_defaults_and_file(self) -> None:
        defaults = load_config(self.runtime / "absent.json")
        self.assertEqual(defaults["default_worker"], "echo")
        self.assertEqual(defaults["timeout_seconds"], 300.0)

        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps({"default_worker": "claude", "timeout_seconds": 9}))
        loaded = load_config(path)
        self.assertEqual(loaded["default_worker"], "claude")
        self.assertEqual(loaded["timeout_seconds"], 9.0)
        self.assertEqual(loaded["runtime_dir"], "runtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
