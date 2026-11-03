#!/usr/bin/env python3
"""Iteration 2 tests: result contract, tool boundary, binary resolution,
heartbeats and stale-ticket recovery."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite import (  # noqa: E402
    ClaudeCodeWorker,
    Router,
    TicketStatus,
    TicketStore,
    check_contract,
    load_config,
    parse_result_line,
    resolve_claude_binary,
    sanitize_text,
)
from superagent_lite.cli import main as cli_main  # noqa: E402
from superagent_lite.models import Ticket, utc_now  # noqa: E402
from superagent_lite.result import build_prompt, normalize_artifact  # noqa: E402
from superagent_lite.workers.base import Worker, WorkerResult, build_env  # noqa: E402
from tests.support import (  # noqa: E402
    make_stub_claude,
    set_stub_state,
    stub_record,
)

CONTRACT_CONFIG_KEYS = {
    "stale_seconds": 300,
    "heartbeat_seconds": 15,
}


def make_config(runtime_dir: Path, **overrides) -> dict:
    config = {
        "default_worker": "echo",
        "claude_binary": "claude",
        "claude_model": None,
        "claude_extra_args": [],
        "claude_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "claude_max_turns": 25,
        "timeout_seconds": 30,
        "runtime_dir": str(runtime_dir),
    }
    config.update(CONTRACT_CONFIG_KEYS)
    config.update(overrides)
    return config


class TempEnvCase(unittest.TestCase):
    """Shared temp runtime directory and secret environment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.bin_dir = self.root / "bin"
        self.stub = make_stub_claude(self.bin_dir)
        self.config = make_config(self.runtime, claude_bin=str(self.stub))
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "SUPERAGENT_TEST_PASSTHROUGH": "passthrough-ok",
                "FAKE_SERVICE_TOKEN": "s3cr3t-token-value",
                "FAKE_SERVICE_SECRET_NAME": "another-secret-value",
            },
        )
        self._env_patch.start()
        self.router = Router(config=self.config)

    def tearDown(self) -> None:
        self.router.close()
        self._env_patch.stop()
        self._tmp.cleanup()

    def run_stub(self, mode: str, **state) -> Ticket:
        set_stub_state(self.bin_dir, mode=mode, **state)
        return self.router.submit("do the task", worker="claude")


# ---------------------------------------------------------------------------
# Parsing the contract
# ---------------------------------------------------------------------------
class ParseResultTests(unittest.TestCase):
    def test_success_line_parsed(self) -> None:
        output = (
            "Did the work.\n"
            'RESULT {"status": "success", "summary": "wrote answer.txt", '
            '"artifacts": ["answer.txt"]}'
        )
        contract, error = parse_result_line(output)
        self.assertIsNone(error)
        self.assertEqual(contract.status, "success")
        self.assertEqual(contract.summary, "wrote answer.txt")
        self.assertEqual(contract.artifacts, ["answer.txt"])

    def test_failed_line_parsed(self) -> None:
        contract, error = parse_result_line(
            'RESULT {"status": "failed", "summary": "permission denied", "artifacts": []}'
        )
        self.assertIsNone(error)
        self.assertEqual(contract.status, "failed")

    def test_last_result_line_wins(self) -> None:
        output = (
            'RESULT {"status": "failed", "summary": "first attempt", "artifacts": []}\n'
            'RESULT {"status": "success", "summary": "second attempt", "artifacts": []}'
        )
        contract, _ = parse_result_line(output)
        self.assertEqual(contract.summary, "second attempt")

    def test_code_fence_around_result_is_tolerated(self) -> None:
        contract, error = parse_result_line(
            '```\nRESULT {"status": "success", "summary": "ok", "artifacts": []}\n```'
        )
        self.assertIsNone(error)
        self.assertEqual(contract.status, "success")

    def test_missing_line(self) -> None:
        contract, error = parse_result_line("I need permissions, please grant them.")
        self.assertIsNone(contract)
        self.assertIn("missing RESULT", error)

    def test_invalid_json(self) -> None:
        contract, error = parse_result_line("RESULT {nope}")
        self.assertIsNone(contract)
        self.assertIn("invalid RESULT JSON", error)

    def test_empty_payload(self) -> None:
        contract, error = parse_result_line("RESULT")
        self.assertIsNone(contract)
        self.assertIn("no JSON payload", error)

    def test_unknown_status_rejected(self) -> None:
        contract, error = parse_result_line('RESULT {"status": "ok"}')
        self.assertIsNone(contract)
        self.assertIn("status", error)

    def test_artifacts_must_be_strings(self) -> None:
        contract, error = parse_result_line(
            'RESULT {"status": "success", "artifacts": [1]}'
        )
        self.assertIsNone(contract)
        self.assertIn("artifacts", error)

    def test_check_contract_verdicts(self) -> None:
        ok = check_contract(
            'RESULT {"status": "success", "summary": "done", "artifacts": ["a.txt"]}', 0
        )
        self.assertTrue(ok.success)
        self.assertEqual(ok.artifacts, ["a.txt"])

        reported = check_contract(
            'RESULT {"status": "failed", "summary": "too hard", "artifacts": []}', 0
        )
        self.assertFalse(reported.success)
        self.assertEqual(reported.error, "too hard")

        missing = check_contract("all done, trust me", 0)
        self.assertFalse(missing.success)
        self.assertIn("missing RESULT", missing.error)

        nonzero = check_contract(
            'RESULT {"status": "success", "summary": "done", "artifacts": []}', 2
        )
        self.assertFalse(nonzero.success)
        self.assertIn("code 2", nonzero.error)

    def test_prompt_contains_contract(self) -> None:
        prompt = build_prompt("create answer.txt", "/tmp/work/x")
        self.assertIn("create answer.txt", prompt)
        self.assertIn("RESULT", prompt)
        self.assertIn("/tmp/work/x", prompt)

    def test_normalize_artifact(self) -> None:
        workdir = "/tmp/work/ticket"
        self.assertEqual(normalize_artifact("./answer.txt", workdir), "answer.txt")
        self.assertEqual(
            normalize_artifact("/tmp/work/ticket/sub/a.txt", workdir), "sub/a.txt"
        )
        self.assertEqual(normalize_artifact("../escape.txt", workdir), "../escape.txt")


# ---------------------------------------------------------------------------
# End-to-end contract through the router with a stub claude
# ---------------------------------------------------------------------------
class ContractRouterTests(TempEnvCase):
    def test_success_creates_file_and_records_artifact(self) -> None:
        ticket = self.run_stub("success", content="chain-ok")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.exit_code, 0)
        self.assertEqual(ticket.artifacts, ["answer.txt"])
        self.assertEqual(ticket.result_summary, "created answer.txt")
        self.assertEqual((Path(ticket.workdir) / "answer.txt").read_text(), "chain-ok")
        self.assertEqual(ticket.attempts, 1)
        self.assertIsNotNone(ticket.started_at)
        self.assertIsNotNone(ticket.heartbeat_at)
        self.assertEqual(self.router.get(ticket.id).artifacts, ["answer.txt"])

    def test_status_failed_is_a_failed_ticket(self) -> None:
        ticket = self.run_stub("failed")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("cannot write outside", ticket.error)

    def test_missing_result_line_is_a_failure_even_with_exit_zero(self) -> None:
        ticket = self.run_stub("missing")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("missing RESULT", ticket.error)
        self.assertEqual(ticket.exit_code, 0)

    def test_invalid_result_json_is_a_failure(self) -> None:
        ticket = self.run_stub("garbage")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("invalid RESULT JSON", ticket.error)

    def test_nonzero_exit_is_a_failure(self) -> None:
        ticket = self.run_stub("nonzero")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("exited with code 3", ticket.error)

    def test_unknown_status_is_a_failure(self) -> None:
        ticket = self.run_stub("wrong_status")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertIn("status", ticket.error)

    def test_escape_attempt_scoped_to_workdir_still_judged_by_contract(self) -> None:
        # A worker that writes outside its workdir can only report success if
        # the OS lets it; the artifact path is recorded as reported (and never
        # silently resolves inside the workdir).
        target = str(self.root / "escaped.txt")
        ticket = self.run_stub("escape", target=target)
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.artifacts, [target])

    def test_show_lists_artifacts(self) -> None:
        ticket = self.run_stub("success", content="chain-ok")
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--config", str(config_path), "show", ticket.id, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["artifacts"], ["answer.txt"])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--config", str(config_path), "show", ticket.id])
        self.assertEqual(code, 0)
        self.assertIn("answer.txt", buffer.getvalue())

    def test_echo_worker_still_succeeds(self) -> None:
        ticket = self.router.submit("echo this", worker="echo")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.artifacts, [])


# ---------------------------------------------------------------------------
# Tool authorization, environment allow-list and redaction
# ---------------------------------------------------------------------------
class BoundaryTests(TempEnvCase):
    def test_default_tools_and_limits_on_command_line(self) -> None:
        ticket = self.run_stub("success")
        record = stub_record(self.bin_dir)
        argv = record["argv"]
        self.assertIn("--allowedTools", argv)
        tools = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(tools, "Read,Write,Edit,Bash,Glob,Grep")
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], ticket.workdir)
        self.assertIn("--max-turns", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "25")
        self.assertIn("RESULT", argv[argv.index("-p") + 1])
        self.assertEqual(Path(record["cwd"]), Path(ticket.workdir))

    def test_configured_tools_and_max_turns_are_used(self) -> None:
        self.router.close()
        config = make_config(
            self.runtime,
            claude_bin=str(self.stub),
            claude_allowed_tools=["Read", "Write"],
            claude_max_turns=7,
        )
        self.router = Router(config=config)
        self.run_stub("success")
        argv = stub_record(self.bin_dir)["argv"]
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Write")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")

    def test_child_env_is_an_allow_list(self) -> None:
        self.router.close()
        config = make_config(
            self.runtime,
            claude_bin=str(self.stub),
            claude_env_passthrough=["PATH", "HOME", "LANG", "SUPERAGENT_TEST_PASSTHROUGH"],
        )
        self.router = Router(config=config)
        self.run_stub("success")
        child_env = stub_record(self.bin_dir)["env"]
        self.assertEqual(child_env.get("SUPERAGENT_TEST_PASSTHROUGH"), "passthrough-ok")
        self.assertIn("PATH", child_env)
        # Credentials from the parent process are not forwarded.
        self.assertNotIn("FAKE_SERVICE_TOKEN", child_env)
        self.assertNotIn("FAKE_SERVICE_SECRET_NAME", child_env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child_env)

    def test_build_env_never_forwards_unnamed_variables(self) -> None:
        base = {"PATH": "/bin", "HOME": "/home/x", "SECRET_TOKEN": "v"}
        env = build_env(["PATH", "HOME"], base=base)
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/home/x"})

    def test_build_env_always_has_path(self) -> None:
        env = build_env([], base={"HOME": "/home/x"})
        self.assertIn("PATH", env)

    def test_secrets_redacted_in_logs(self) -> None:
        ticket = self.router.submit(
            "print secrets", worker=EchoWorkerForTest()
        )
        self.assertEqual(ticket.status, TicketStatus.DONE)

        set_stub_state(
            self.bin_dir,
            mode="success",
            stdout="token is s3cr3t-token-value and key is sk-abcdef123456",
        )
        claude_ticket = self.router.submit("leak check", worker="claude")
        log = Path(claude_ticket.log_path).read_text(encoding="utf-8")
        self.assertIn("***", log)
        self.assertNotIn("s3cr3t-token-value", log)
        self.assertNotIn("sk-abcdef123456", log)

    def test_sanitize_text(self) -> None:
        env = {
            "OPENAI_API_KEY": "plain-api-key-value",
            "GITHUB_TOKEN": "ghp-raw-token",
            "MY_APP_SECRET_KEY": "topsecret",
            "MONKEY": "banana",
        }
        text = "a plain-api-key-value b ghp-raw-token c topsecret d banana e sk-zzzzzz"
        cleaned = sanitize_text(text, env)
        self.assertNotIn("plain-api-key-value", cleaned)
        self.assertNotIn("ghp-raw-token", cleaned)
        self.assertNotIn("topsecret", cleaned)
        self.assertIn("banana", cleaned)  # not a secret-looking name
        self.assertNotIn("sk-zzzzzz", cleaned)
        self.assertEqual(cleaned.count("***"), 4)


class EchoWorkerForTest(Worker):
    name = "leaky-echo"

    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        return WorkerResult(success=True, summary="ok", exit_code=0)


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------
class BinaryResolutionTests(unittest.TestCase):
    def test_explicit_config_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_claude_binary(
                {"claude_bin": "~/custom/claude", "claude_binary": "claude"}
            )
            self.assertEqual(resolved, str(Path("~/custom/claude").expanduser()))
            env = {"PATH": tmp}
            resolved = resolve_claude_binary(
                {"claude_bin": "/opt/claude", "claude_binary": "claude"}, env=env
            )
            self.assertEqual(resolved, "/opt/claude")

    def test_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub_claude(Path(tmp))
            # A private home without ~/.local/bin/claude proves PATH won.
            empty_home = Path(tmp) / "home"
            resolved = resolve_claude_binary(
                {"claude_bin": None, "claude_binary": "claude"},
                env={"PATH": tmp},
                home=empty_home,
            )
            self.assertEqual(resolved, str(stub))

    def test_home_local_bin_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".local" / "bin" / "claude"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            resolved = resolve_claude_binary(
                {"claude_bin": None, "claude_binary": "claude"},
                env={"PATH": str(home / "empty")},
                home=home,
            )
            self.assertEqual(resolved, str(target))

    def test_not_found_falls_back_to_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_claude_binary(
                {"claude_bin": None, "claude_binary": "claude"},
                env={"PATH": str(Path(tmp) / "empty")},
                home=Path(tmp),
            )
            self.assertEqual(resolved, "claude")

    def test_worker_reports_clear_error_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = Router(
                config=make_config(Path(tmp) / "runtime", claude_bin="/nonexistent/claude")
            )
            try:
                ticket = router.submit("task", worker="claude")
            finally:
                router.close()
            self.assertEqual(ticket.status, TicketStatus.FAILED)
            self.assertIn("not found", ticket.error)
            self.assertIn("claude_bin", ticket.error)


# ---------------------------------------------------------------------------
# Heartbeats, attempts and stale recovery
# ---------------------------------------------------------------------------
class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name) / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_heartbeat_updates_only_running_tickets(self) -> None:
        with TicketStore(self.runtime / "a.db") as store:
            ticket = Ticket(id="t1", description="x", worker="echo")
            store.create(ticket)
            self.assertIsNone(store.heartbeat("t1"))
            store.set_status("t1", "running", heartbeat_at="2000-01-01T00:00:00+00:00")
            updated = store.heartbeat("t1")
            self.assertIsNotNone(updated)
            self.assertNotEqual(updated.heartbeat_at, "2000-01-01T00:00:00+00:00")
            store.set_status("t1", "done")
            self.assertIsNone(store.heartbeat("t1"))

    def test_store_recovers_stale_and_keeps_live_running(self) -> None:
        path = self.runtime / "b.db"
        with TicketStore(path) as store:
            stale = Ticket(id="stale", description="x", worker="echo")
            live = Ticket(id="live", description="y", worker="echo")
            store.create(stale)
            store.create(live)
            store.set_status("stale", "running", heartbeat_at="2000-01-01T00:00:00+00:00")
            store.set_status("live", "running", heartbeat_at=utc_now())
        with TicketStore(path, stale_seconds=300) as store:
            self.assertEqual(store.get("stale").status, TicketStatus.FAILED)
            self.assertIn("heartbeat", store.get("stale").error)
            self.assertEqual(store.get("live").status, TicketStatus.RUNNING)
            self.assertEqual([t.id for t in store.recovered], ["stale"])

    def test_running_without_heartbeat_is_recovered(self) -> None:
        path = self.runtime / "c.db"
        with TicketStore(path) as store:
            store.create(Ticket(id="legacy", description="x", worker="echo"))
            store.set_status("legacy", "running", heartbeat_at=None)
        with TicketStore(path) as store:
            self.assertEqual(store.get("legacy").status, TicketStatus.FAILED)
            self.assertIn("interrupted", store.get("legacy").error)

    def test_router_recovery_writes_log_note(self) -> None:
        config = make_config(self.runtime)
        with Router(config=config) as router:
            ticket = router.submit("work", worker="echo")
            router.store.set_status(
                ticket.id, "running", heartbeat_at="2000-01-01T00:00:00+00:00"
            )
        with Router(config=config) as router:
            loaded = router.get(ticket.id)
            self.assertEqual(loaded.status, TicketStatus.FAILED)
        log = Path(ticket.log_path).read_text(encoding="utf-8")
        self.assertIn("recovered", log)

    def test_recent_running_ticket_survives_restart(self) -> None:
        config = make_config(self.runtime)
        with Router(config=config) as router:
            ticket = router.submit("work", worker="echo")
            router.store.set_status(ticket.id, "running", heartbeat_at=utc_now())
        with Router(config=config) as router:
            self.assertEqual(router.get(ticket.id).status, TicketStatus.RUNNING)

    def test_router_heartbeats_during_a_long_worker_run(self) -> None:
        seen: list[str] = []

        class SlowWorker(Worker):
            name = "slow"

            def run(self, ticket, workdir, on_heartbeat=None):
                time.sleep(0.25)
                stored = store.get(ticket.id)
                seen.append(stored.heartbeat_at or "")
                return WorkerResult(success=True, summary="slow", exit_code=0)

        config = make_config(self.runtime, heartbeat_seconds=0.05)
        with Router(config=config) as router:
            store = router.store
            worker = SlowWorker()
            ticket = router.submit("long task", worker=worker)
        self.assertEqual(ticket.status, TicketStatus.DONE)
        # The router ticked while the worker was sleeping, so the row's
        # heartbeat moved past the dispatch-time value.
        self.assertTrue(seen and seen[0])
        self.assertGreater(seen[0], ticket.started_at)
        log = Path(ticket.log_path).read_text(encoding="utf-8")
        self.assertIn("heartbeat", log)

    def test_adapter_callback_receives_ticks(self) -> None:
        class CallingWorker(Worker):
            name = "calling"

            def run(self, ticket, workdir, on_heartbeat=None):
                ticks = []
                for _ in range(5):
                    time.sleep(0.03)
                    if on_heartbeat:
                        on_heartbeat()
                        ticks.append(1)
                return WorkerResult(success=True, summary=f"{len(ticks)} beats", exit_code=0)

        config = make_config(self.runtime, heartbeat_seconds=60)
        with Router(config=config) as router:
            ticket = router.submit("beat", worker=CallingWorker())
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.result_summary, "5 beats")
        log = Path(ticket.log_path).read_text(encoding="utf-8")
        self.assertIn("[router] heartbeat", log)

    def test_config_validates_new_keys(self) -> None:
        path = self.runtime / "config.json"
        path.write_text(json.dumps({"claude_max_turns": 0}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)
        path.write_text(json.dumps({"stale_seconds": -1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)
        path.write_text(json.dumps({"claude_allowed_tools": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_claude_worker_command_shape(self) -> None:
        worker = ClaudeCodeWorker(binary="claude", allowed_tools=["Read"], max_turns=3)
        command = worker.build_command("hello", "/tmp/work")
        self.assertEqual(command[0], worker.resolved_binary())
        self.assertIn("hello", command[2])
        self.assertIn("RESULT", command[2])
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read")
        self.assertEqual(command[command.index("--max-turns") + 1], "3")
        self.assertEqual(command[command.index("--add-dir") + 1], "/tmp/work")


if __name__ == "__main__":
    unittest.main(verbosity=2)
