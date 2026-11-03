#!/usr/bin/env python3
"""Phase 3 milestone B tests: Brain/planner, chat REPL, HTTP API, memory.

Everything here runs against a local stub ``/v1/messages`` server
(:class:`tests.support.StubPlanner`) -- no network, no credentials, no Claude
Code binary.  The end-to-end path is still real: the Brain enqueues tickets
into SQLite and a real :class:`~superagent_lite.daemon.Daemon` claims and
executes them with a stub worker.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite import (  # noqa: E402
    Brain,
    Daemon,
    Endpoint,
    MessagesClient,
    PlannerConfigError,
    PlannerError,
    Router,
    TicketStatus,
)
from superagent_lite.brain import (  # noqa: E402
    MODE_ERROR,
    MODE_PENDING,
    MODE_REPLY,
    MODE_SUMMARY,
    parse_plan,
)
from superagent_lite.cli import main as cli_main  # noqa: E402
from superagent_lite.http_api import (  # noqa: E402
    DEFAULT_MAX_BODY,
    ApiConfig,
    make_server,
    token_from_env,
)
from superagent_lite.memory import read_memory  # noqa: E402
from superagent_lite.planner import load_endpoint  # noqa: E402
from superagent_lite.workers.base import Worker, WorkerResult  # noqa: E402
from tests.support import StubPlanner, wait_for  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PLAN_ONE = json.dumps(
    {
        "action": "plan",
        "workdir": None,
        "tickets": [
            {
                "id": "t1",
                "description": "create hello.txt containing 'hello'",
                "worker": "stub",
                "depends_on": [],
            }
        ],
    }
)

PLAN_TWO = json.dumps(
    {
        "action": "plan",
        "workdir": None,
        "tickets": [
            {"id": "t1", "description": "first step", "worker": "stub", "depends_on": []},
            {
                "id": "t2",
                "description": "second step",
                "worker": "stub",
                "depends_on": ["t1"],
            },
        ],
    }
)


def brain_config(runtime_dir: Path, settings: Path, **overrides) -> dict:
    config = {
        "default_worker": "echo",
        "claude_bin": None,
        "claude_model": None,
        "runtime_dir": str(runtime_dir),
        "timeout_seconds": 10,
        "stale_seconds": 60,
        "heartbeat_seconds": 0.2,
        "max_retries": 0,
        "daemon_concurrency": 2,
        "daemon_poll_seconds": 0.02,
        "daemon_watchdog_seconds": 0.2,
        "daemon_shutdown_grace_seconds": 5,
        "brain_model": "stub-planner-model",
        "brain_settings_path": str(settings),
        "brain_worker": "stub",
        "chat_timeout_seconds": 10,
        "chat_poll_seconds": 0.02,
        "brain_memory_max_chars": 2000,
        "memory_path": str(runtime_dir.parent / "memory.md"),
    }
    config.update(overrides)
    return config


class RecordingWorker(Worker):
    """Deterministic worker that writes a file and honours the contract."""

    result_contract = False

    def __init__(self, name: str = "stub", fail: bool = False) -> None:
        # ``name`` is a class attribute on the real adapters; an instance
        # attribute lets one test register two behaviours side by side.
        self.name = name
        self.fail = fail
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def run(self, ticket, workdir: Path) -> WorkerResult:
        with self._lock:
            self.seen.append(ticket.description)
        if self.fail:
            return WorkerResult(
                success=False, summary="", error="simulated worker failure"
            )
        name = "hello.txt"
        (workdir / name).write_text(f"done: {ticket.description}", encoding="utf-8")
        return WorkerResult(
            success=True,
            summary=f"wrote {name}",
            exit_code=0,
            artifacts=[name],
        )


class BrainCase(unittest.TestCase):
    """Temp runtime, stub planner server and a scripted responder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.settings = self.root / "settings.json"
        self.planner = StubPlanner().start()
        self.planner.settings_file(self.settings)
        self.worker = RecordingWorker()
        self.worker_fail = RecordingWorker(name="stub_fail", fail=True)
        self.config = brain_config(self.runtime, self.settings)
        self._daemons: list[tuple[Daemon, threading.Thread]] = []
        self._brains: list[Brain] = []
        self._stores: list = []

    def tearDown(self) -> None:
        for brain in self._brains:
            brain.close()
        for daemon, thread in self._daemons:
            daemon.request_stop("teardown")
            thread.join(timeout=10)
        for store in self._stores:
            with contextlib.suppress(Exception):
                store.close()
        self.planner.stop()
        self._tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def script(self, *answers: str) -> None:
        """Make the planner return ``answers`` in order (then the last one)."""

        def responder(_payload, index):
            return answers[min(index, len(answers) - 1)]

        self.planner.responder = responder

    def brain(self, config: dict | None = None) -> Brain:
        brain = Brain(
            config=config or self.config,
            router=Router(
                config=config or self.config,
                workers={"stub": self.worker, "stub_fail": self.worker_fail},
                recover_mode="none",
            ),
        )
        self._brains.append(brain)
        return brain

    def start_daemon(self, config: dict | None = None) -> Daemon:
        daemon = Daemon(
            config=config or self.config,
            workers={"stub": self.worker, "stub_fail": self.worker_fail},
            log=lambda line: None,
        )
        thread = threading.Thread(target=daemon.run, name="brain-test-daemon", daemon=True)
        thread.start()
        self._daemons.append((daemon, thread))
        wait_for(lambda: daemon.router is not None)
        return daemon

    def ticket(self, ticket_id: str):
        with Router(config=self.config, recover_mode="none") as router:
            return router.get(ticket_id)


class PlannerProtocolTests(BrainCase):
    def test_endpoint_is_resolved_from_settings_json(self) -> None:
        endpoint = load_endpoint(self.settings, environ={})
        self.assertEqual(endpoint.base_url, self.planner.base_url)
        self.assertEqual(endpoint.token, self.planner.token)
        self.assertNotIn(self.planner.token, repr(endpoint))

    def test_environment_is_a_fallback_for_settings(self) -> None:
        endpoint = load_endpoint(
            self.root / "absent.json",
            environ={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                "ANTHROPIC_AUTH_TOKEN": "env-token",
            },
        )
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:9")
        self.assertEqual(endpoint.token, "env-token")

    def test_missing_credentials_raise_a_config_error(self) -> None:
        with self.assertRaises(PlannerConfigError):
            load_endpoint(self.root / "absent.json", environ={})

    def test_messages_url_handles_base_urls_with_and_without_v1(self) -> None:
        self.assertEqual(
            Endpoint("https://gw.example", "t").messages_url,
            "https://gw.example/v1/messages",
        )
        self.assertEqual(
            Endpoint("https://gw.example/v1/", "t").messages_url,
            "https://gw.example/v1/messages",
        )

    def test_client_posts_anthropic_shape_and_reads_text_blocks(self) -> None:
        client = MessagesClient(
            Endpoint(self.planner.base_url, self.planner.token), timeout=5
        )
        self.planner.responder = lambda _payload, _index: "plain answer"
        text = client.complete(
            model="m",
            system="system prompt",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=32,
        )
        self.assertEqual(text, "plain answer")
        payload = self.planner.payloads()[-1]
        self.assertEqual(payload["model"], "m")
        self.assertEqual(payload["system"], "system prompt")
        self.assertEqual(payload["max_tokens"], 32)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])
        headers = self.planner.headers()[-1]
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["x-api-key"], self.planner.token)

    def test_token_never_appears_in_an_http_error(self) -> None:
        self.planner.status = 401
        self.planner.error_body = "denied"
        client = MessagesClient(
            Endpoint(self.planner.base_url, self.planner.token), timeout=5
        )
        with self.assertRaises(PlannerError) as caught:
            client.complete(model="m", system="", messages=[], max_tokens=8)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(self.planner.token, str(caught.exception))

    def test_planner_errors_surface_as_a_chat_reply_not_a_crash(self) -> None:
        self.planner.status = 500
        reply = self.brain().reply("hello")
        self.assertEqual(reply.mode, MODE_ERROR)
        self.assertIn("planner", reply.text)
        self.assertNotIn(self.planner.token, reply.text)


class PlanParsingTests(unittest.TestCase):
    def test_plain_json_reply(self) -> None:
        plan = parse_plan('{"action": "reply", "reply": "Bonjour"}')
        self.assertTrue(plan.is_reply)
        self.assertEqual(plan.reply, "Bonjour")

    def test_plan_with_dependencies(self) -> None:
        plan = parse_plan(PLAN_TWO)
        self.assertEqual(plan.action, "plan")
        self.assertEqual([step.id for step in plan.steps], ["t1", "t2"])
        self.assertEqual(plan.steps[1].depends_on, ["t1"])

    def test_fenced_json_is_tolerated(self) -> None:
        plan = parse_plan(f"```json\n{PLAN_ONE}\n```")
        self.assertEqual(plan.action, "plan")
        self.assertEqual(len(plan.steps), 1)

    def test_prose_answer_falls_back_to_a_reply(self) -> None:
        plan = parse_plan("Just a normal sentence.")
        self.assertTrue(plan.is_reply)
        self.assertEqual(plan.reply, "Just a normal sentence.")

    def test_invalid_plan_shapes_are_rejected(self) -> None:
        for bad in (
            '{"action": "plan"}',
            '{"action": "plan", "tickets": []}',
            '{"action": "plan", "tickets": [{"description": ""}]}',
            '{"action": "plan", "tickets": [{"id": "t1", "description": "a", '
            '"depends_on": ["missing"]}]}',
            '{"action": "plan", "tickets": [{"id": "a", "description": "a", '
            '"depends_on": ["b"]}, {"id": "b", "description": "b", '
            '"depends_on": ["a"]}]}',
            '{"action": "reply"}',
            '{"action": "dance"}',
            '{"action": "plan", "tickets": [broken',
        ):
            with self.subTest(bad=bad), self.assertRaises(PlannerError):
                parse_plan(bad)

    def test_ticket_limit_is_enforced(self) -> None:
        plan = {
            "action": "plan",
            "tickets": [{"id": f"t{i}", "description": f"step {i}"} for i in range(5)],
        }
        with self.assertRaises(PlannerError):
            parse_plan(json.dumps(plan), max_tickets=4)


class BrainBehaviourTests(BrainCase):
    def test_direct_reply_path_creates_no_tickets(self) -> None:
        self.script('{"action": "reply", "reply": "你好，我是你的常驻助手。"}')
        brain = self.brain()
        reply = brain.reply("你好")
        self.assertEqual(reply.mode, MODE_REPLY)
        self.assertEqual(reply.text, "你好，我是你的常驻助手。")
        self.assertEqual(reply.ticket_ids, [])
        self.assertEqual(brain.router.store.count(), 0)
        self.assertEqual(brain.router.store.count(TicketStatus.PENDING), 0)

    def test_decomposition_creates_dependent_tickets_and_summarizes(self) -> None:
        self.script(PLAN_TWO, "SUMMARY: both steps finished.")
        daemon = self.start_daemon()
        reply = self.brain().reply("do two things")

        self.assertEqual(reply.mode, MODE_SUMMARY, reply.text)
        self.assertEqual(reply.text, "SUMMARY: both steps finished.")
        self.assertEqual(len(reply.ticket_ids), 2)

        first = self.ticket(reply.ticket_ids[0])
        second = self.ticket(reply.ticket_ids[1])
        self.assertEqual(first.status, TicketStatus.DONE, first.error)
        self.assertEqual(second.status, TicketStatus.DONE, second.error)
        self.assertEqual(second.depends_on, [first.id])
        self.assertEqual(second.worker, "stub")
        # Both tickets of one request share a working directory.
        self.assertEqual(first.workdir, second.workdir)
        # The dependency really gated execution: t1 finished before t2 began.
        self.assertLessEqual(first.finished_at, second.started_at)

        # Planning used the configured cheap model; the summary prompt carried
        # the workers' structured results.
        payloads = self.planner.payloads()
        self.assertEqual(payloads[0]["model"], "stub-planner-model")
        summary_prompt = payloads[-1]["messages"][0]["content"]
        self.assertIn('"status": "done"', summary_prompt)
        self.assertIn("hello.txt", summary_prompt)
        daemon.request_stop("test done")

    def test_dependency_failure_cascades_and_is_reported_honestly(self) -> None:
        self.script(
            json.dumps(
                {
                    "action": "plan",
                    "tickets": [
                        {"id": "a", "description": "will fail", "worker": "stub_fail"},
                        {
                            "id": "b",
                            "description": "needs a",
                            "worker": "stub",
                            "depends_on": ["a"],
                        },
                        # A ticket that does not depend on the failure still runs.
                        {"id": "c", "description": "independent", "worker": "stub"},
                    ],
                }
            ),
            "SUMMARY: step a failed, b was not run.",
        )
        daemon = self.start_daemon()
        reply = self.brain().reply("do the failing thing")
        self.assertEqual(reply.mode, MODE_SUMMARY)
        first, second, independent = (self.ticket(i) for i in reply.ticket_ids)
        self.assertEqual(first.status, TicketStatus.FAILED)
        self.assertEqual(second.status, TicketStatus.FAILED)
        self.assertIn("dependency failed", second.error)
        self.assertEqual(independent.status, TicketStatus.DONE)
        # The dependent never ran; the independent step did.
        self.assertNotIn("needs a", self.worker.seen)
        self.assertIn("independent", self.worker.seen)
        summary_prompt = self.planner.payloads()[-1]["messages"][0]["content"]
        self.assertIn("simulated worker failure", summary_prompt)
        daemon.request_stop("test done")

    def test_timeout_returns_ticket_ids_instead_of_waiting_forever(self) -> None:
        self.script(PLAN_ONE)
        brain = self.brain()
        # No daemon runs, so the ticket cannot finish.
        reply = brain.reply("create hello.txt", timeout=0.2)
        self.assertEqual(reply.mode, MODE_PENDING)
        self.assertTrue(reply.timed_out)
        self.assertEqual(len(reply.ticket_ids), 1)
        self.assertIn(reply.ticket_ids[0], reply.text)
        self.assertEqual(len(self.planner.payloads()), 1)  # no summary call

    def test_memory_is_injected_into_the_planner_prompt(self) -> None:
        memory = self.root / "memory.md"
        memory.write_text("User prefers Chinese replies.\nProject: superagent.", encoding="utf-8")
        self.script('{"action": "reply", "reply": "ok"}')
        config = dict(self.config, memory_path=str(memory))
        self.brain(config).reply("hi")
        system = self.planner.payloads()[0]["system"]
        self.assertIn("User prefers Chinese replies.", system)
        self.assertIn("Project: superagent.", system)

    def test_memory_truncation_keeps_the_beginning(self) -> None:
        path = self.root / "long.md"
        path.write_text("A" * 50 + "B" * 5000, encoding="utf-8")
        text = read_memory(path, max_chars=100)
        self.assertLessEqual(len(text), 100 + len("\n...(memory truncated; see memory.md)"))
        self.assertTrue(text.startswith("A" * 50))
        self.assertIn("truncated", text)
        self.assertEqual(read_memory(self.root / "absent.md"), "")

    def test_invalid_worker_name_falls_back_to_the_configured_worker(self) -> None:
        self.script(
            json.dumps(
                {
                    "action": "plan",
                    "tickets": [{"id": "t1", "description": "x", "worker": "nope"}],
                }
            ),
            "SUMMARY: done",
        )
        self.start_daemon()
        reply = self.brain().reply("x")
        self.assertEqual(self.ticket(reply.ticket_ids[0]).worker, "stub")

    def test_chat_timeout_is_configurable_per_call(self) -> None:
        self.script(PLAN_ONE)
        brain = self.brain()
        reply = brain.reply("slow", timeout=0.05)
        self.assertEqual(reply.mode, MODE_PENDING)

    def test_absolute_workdir_from_the_planner_is_used(self) -> None:
        target = self.root / "user dir"
        target.mkdir()
        self.script(
            json.dumps(
                {
                    "action": "plan",
                    "workdir": str(target),
                    "tickets": [{"id": "t1", "description": "write there", "worker": "stub"}],
                }
            ),
            "SUMMARY: wrote",
        )
        self.start_daemon()
        reply = self.brain().reply("write in my dir")
        ticket = self.ticket(reply.ticket_ids[0])
        self.assertEqual(Path(ticket.workdir), target.resolve())
        self.assertTrue((target / "hello.txt").is_file())


class ChatCliTests(BrainCase):
    def run_cli(self, argv: list[str], stdin_text: str) -> tuple[int, str, str]:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            code = cli_main(["--config", str(config_path)] + argv)
        return code, out.getvalue(), err.getvalue()

    def test_chat_repl_reads_messages_and_prints_the_reply(self) -> None:
        self.script(
            '{"action": "reply", "reply": "Hi there!"}',
            '{"action": "reply", "reply": "Still me."}',
        )
        code, out, err = self.run_cli(["chat"], "hello\nsecond message\n/quit\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip().splitlines(), ["Hi there!", "Still me."])
        self.assertEqual(len(self.planner.payloads()), 2)

    def test_chat_repl_end_of_input_exits_cleanly(self) -> None:
        self.script('{"action": "reply", "reply": "bye"}')
        code, out, _ = self.run_cli(["chat"], "hi\n")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "bye")

    def test_chat_tickets_path_via_cli(self) -> None:
        # The CLI builds its own router from config, so use the offline echo
        # worker: the point here is the plumbing, not the worker's side effects.
        self.config = dict(self.config, brain_worker="echo")
        self.script(
            json.dumps(
                {
                    "action": "plan",
                    "tickets": [{"id": "t1", "description": "echo step", "worker": "echo"}],
                }
            ),
            "SUMMARY: hello.txt created.",
        )
        self.start_daemon()
        code, out, err = self.run_cli(["chat", "--timeout", "10"], "make hello.txt\n")
        self.assertEqual(code, 0, err)
        self.assertIn("hello.txt created.", out)
        self.assertIn("mode=summary", err)

    def test_chat_without_credentials_reports_a_planner_error(self) -> None:
        config = dict(self.config, brain_settings_path=str(self.root / "absent.json"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.config = config
            code, out, err = self.run_cli(["chat"], "hello\n")
        self.assertEqual(code, 0)
        self.assertIn("planner error", out)
        self.assertIn("ANTHROPIC_BASE_URL", out)


class HttpApiTests(BrainCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = "http-static-token"
        self._servers: list = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        super().tearDown()

    def serve(self, token: str | None, brain: Brain | None = None):
        server = make_server(
            brain or self.brain(),
            ApiConfig(host="127.0.0.1", port=0, token=token),
        )
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self._servers.append(server)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}", thread

    def request(self, url: str, *, payload=None, token: str | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            return exc.code, json.loads(body) if body else {}

    def test_auth_is_rejected_without_the_static_token(self) -> None:
        base, _ = self.serve(self.token)
        self.script('{"action": "reply", "reply": "should not happen"}')
        status, body = self.request(base + "/chat", payload={"text": "hi"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, _ = self.request(base + "/chat", payload={"text": "hi"}, token="wrong")
        self.assertEqual(status, 401)
        # Rejected requests never reached the planner.
        self.assertEqual(self.planner.payloads(), [])

    def test_chat_is_accepted_with_the_static_token(self) -> None:
        base, _ = self.serve(self.token)
        self.script('{"action": "reply", "reply": "authenticated hello"}')
        status, body = self.request(
            base + "/chat", payload={"text": "hi", "user_id": "u1"}, token=self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["reply"], "authenticated hello")
        self.assertEqual(body["mode"], "reply")

    def test_health_is_open_and_ticket_lookup_is_protected(self) -> None:
        base, _ = self.serve(self.token)
        status, body = self.request(base + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["auth"], "token")
        status, _ = self.request(base + "/tickets/unknown")
        self.assertEqual(status, 401)
        status, body = self.request(base + "/tickets/unknown", token=self.token)
        self.assertEqual(status, 404)

    def test_chat_plan_and_ticket_progress_over_http(self) -> None:
        self.script(PLAN_ONE, "SUMMARY: hello.txt created over HTTP.")
        self.start_daemon()
        base, _ = self.serve(self.token)
        status, body = self.request(
            base + "/chat", payload={"text": "create hello.txt"}, token=self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "summary")
        self.assertEqual(len(body["ticket_ids"]), 1)
        ticket_id = body["ticket_ids"][0]

        status, ticket = self.request(base + f"/tickets/{ticket_id}", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(ticket["status"], "done")
        self.assertEqual(ticket["artifacts"], ["hello.txt"])

    def test_timeout_reply_reports_pending_tickets(self) -> None:
        self.script(PLAN_ONE)
        base, _ = self.serve(self.token)
        # No daemon: /chat cannot finish.  Short timeout via a dedicated brain.
        config = dict(self.config, chat_timeout_seconds=0.2)
        server = make_server(
            self.brain(config), ApiConfig(host="127.0.0.1", port=0, token=self.token)
        )
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self._servers.append(server)
        host, port = server.server_address[:2]
        status, body = self.request(
            f"http://{host}:{port}/chat", payload={"text": "slow"}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "pending")
        self.assertTrue(body["timed_out"])
        self.assertEqual(len(body["ticket_ids"]), 1)

    def test_bad_requests_are_400s(self) -> None:
        base, _ = self.serve(None)
        status, body = self.request(base + "/chat", payload={"text": ""})
        self.assertEqual(status, 400)
        status, body = self.request(base + "/chat", payload={"nope": 1})
        self.assertEqual(status, 400)
        status, body = self.request(base + "/nope", payload={"text": "x"})
        self.assertEqual(status, 404)

    def test_oversized_body_is_refused(self) -> None:
        self.assertGreater(DEFAULT_MAX_BODY, 1024)
        brain = self.brain()
        server = make_server(
            brain, ApiConfig(host="127.0.0.1", port=0, token=None, max_body_bytes=64)
        )
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self._servers.append(server)
        host, port = server.server_address[:2]
        status, body = self.request(
            f"http://{host}:{port}/chat", payload={"text": "x" * 500}
        )
        self.assertEqual(status, 413)

    def test_token_env_is_read_and_blank_means_open(self) -> None:
        self.assertEqual(token_from_env({"SUPERAGENT_HTTP_TOKEN": " t "}), "t")
        self.assertIsNone(token_from_env({"SUPERAGENT_HTTP_TOKEN": "  "}))
        self.assertIsNone(token_from_env({}))

    def test_error_reply_maps_to_502(self) -> None:
        self.planner.status = 401
        base, _ = self.serve(None)
        status, body = self.request(base + "/chat", payload={"text": "hi"})
        self.assertEqual(status, 502)
        self.assertEqual(body["mode"], "error")


class DependencyQueueTests(BrainCase):
    """Store-level guarantees that make ``depends_on`` real."""

    def test_claim_skips_tickets_whose_dependencies_are_not_done(self) -> None:
        first = self._enqueue("first", [])
        second = self._enqueue("second", [first.id])
        store = self._store()
        claimed = store.claim_next()
        self.assertEqual(claimed.id, first.id)
        # The dependent is still pending and must not be handed out.
        self.assertIsNone(store.claim_next())
        store.set_status(first.id, TicketStatus.DONE, result_summary="ok")
        claimed = store.claim_next()
        self.assertEqual(claimed.id, second.id)
        store.close()

    def test_unknown_dependency_blocks_claiming(self) -> None:
        ticket = self._enqueue("orphan", ["does-not-exist"])
        store = self._store()
        self.assertIsNone(store.claim_next())
        self.assertIsNone(store.claim(ticket.id))
        store.close()

    def test_watchdog_giving_up_cascades_to_dependents(self) -> None:
        first = self._enqueue("stuck", [])
        second = self._enqueue("depends on stuck", [first.id])
        # Park ``first`` as a dead running lease whose retry budget is spent.
        store = self._store()
        store.claim(first.id, max_retries=0)
        store.set_status(
            first.id,
            TicketStatus.RUNNING,
            touch=False,
            heartbeat_at="2000-01-01T00:00:00+00:00",
        )
        store.close()

        daemon = self.start_daemon()
        failed = wait_for(
            lambda: (
                ticket
                if (ticket := self.ticket(first.id)).status is TicketStatus.FAILED
                else None
            ),
            timeout=10,
        )
        # Depending on timing the daemon's startup recovery or its watchdog
        # gives up first; both leave the same audit trail and must cascade.
        self.assertTrue(
            "watchdog" in failed.error or "interrupted" in failed.error, failed.error
        )
        dependent = wait_for(
            lambda: (
                ticket
                if (ticket := self.ticket(second.id)).status is TicketStatus.FAILED
                else None
            ),
            timeout=10,
        )
        self.assertIn("dependency failed", dependent.error)
        daemon.request_stop("test done")

    def test_failed_dependency_fails_pending_dependents(self) -> None:
        first = self._enqueue("first", [])
        second = self._enqueue("second", [first.id])
        store = self._store()
        store.set_status(first.id, TicketStatus.FAILED, error="boom")
        dependent = next(
            ticket for ticket in store.fail_dependents(first.id) if ticket.id == second.id
        )
        self.assertEqual(dependent.status, TicketStatus.FAILED)
        self.assertIn("dependency failed", dependent.error)
        store.close()

    def _enqueue(self, text: str, depends_on: list[str]):
        with Router(
            config=self.config,
            workers={"stub": self.worker, "stub_fail": self.worker_fail},
            recover_mode="none",
        ) as router:
            return router.enqueue(text, worker="stub", depends_on=depends_on)

    def _store(self):
        """A second connection to the queue, closed at teardown.

        SQLite's default journal mode takes a database-level write lock, so a
        connection kept open here would block the daemon's claims.
        """

        from superagent_lite.store import TicketStore

        store = TicketStore(self.runtime / "superagent.db", recover=False)
        self._stores.append(store)
        return store


if __name__ == "__main__":
    unittest.main(verbosity=2)
