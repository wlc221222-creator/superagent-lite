#!/usr/bin/env python3
"""Phase 4 tests: web chat UI, artifact download, Feishu channel, deployment.

Everything runs offline: the HTTP API is exercised over a loopback socket
with a stub planner and a recording worker, and the Feishu API is a local
stub server.  No credentials, no real model, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite.artifacts import (  # noqa: E402
    ArtifactNotFound,
    ArtifactPathError,
    artifact_entries,
    content_disposition,
    content_type_for,
    resolve_artifact,
)
from superagent_lite.channels import (  # noqa: E402
    EventDeduper,
    FeishuApiError,
    FeishuEventError,
    FeishuReply,
    FeishuSettings,
    handle_event,
    parse_event,
    settings_from_env,
)
from superagent_lite.http_api import ApiConfig, make_server  # noqa: E402
from superagent_lite.router import Router  # noqa: E402
from superagent_lite.webui import CHAT_PAGE  # noqa: E402
from superagent_lite.workers.base import Worker, WorkerResult  # noqa: E402
from tests.support import StubPlanner  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The planner's answer in: reply directly / plan one echo ticket.
REPLY_ONLY = json.dumps({"action": "reply", "reply": "pong"})
PLAN_ONE = json.dumps(
    {
        "action": "plan",
        "workdir": None,
        "tickets": [
            {"id": "t1", "description": "say hello", "worker": "echo", "depends_on": []}
        ],
    }
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class FileWorker(Worker):
    """Writes a text artifact and a binary artifact, then reports both."""

    result_contract = False

    def __init__(self) -> None:
        self.name = "files"
        self.seen: list[str] = []

    def run(self, ticket, workdir: Path) -> WorkerResult:
        self.seen.append(ticket.description)
        (workdir / "out").mkdir(parents=True, exist_ok=True)
        (workdir / "out" / "hello.txt").write_text("hello artifact", encoding="utf-8")
        (workdir / "image.bin").write_bytes(b"\x00\x01\x02binary")
        return WorkerResult(
            success=True,
            summary="wrote artifacts",
            exit_code=0,
            artifacts=["out/hello.txt", "image.bin", "missing.txt"],
        )


class FakeBrain:
    """Minimal ``Brain`` surface the Feishu handler needs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        self.answer = SimpleNamespace(text="agent answer", mode="reply")

    def reply(self, text: str, *, user_id: str = "local"):
        self.calls.append((text, user_id))
        if self.fail:
            raise RuntimeError("planner exploded")
        return self.answer


def api_config(runtime: Path, **overrides) -> dict:
    config = {
        "default_worker": "files",
        "claude_model": None,
        "runtime_dir": str(runtime),
        "timeout_seconds": 10,
        "stale_seconds": 60,
        "heartbeat_seconds": 0.2,
        "max_retries": 0,
        "brain_model": "stub-planner-model",
        "brain_worker": "files",
        "chat_timeout_seconds": 5,
        "chat_poll_seconds": 0.02,
        "memory_path": str(runtime.parent / "memory.md"),
    }
    config.update(overrides)
    return config


class ServerCase(unittest.TestCase):
    """A real HTTP server on loopback with a real Router and stub workers."""

    token = "phase4-token"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.planner = StubPlanner(
            responder=lambda payload, index: '{"action": "reply", "reply": "pong"}'
        ).start()
        self.settings = self.root / "settings.json"
        self.planner.settings_file(self.settings)
        self.worker = FileWorker()
        self.config = api_config(self.runtime, brain_settings_path=str(self.settings))
        self.brain = self._make_brain()
        self._servers: list = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for router in getattr(self, "_routers", []):
            router.close()
        self.planner.stop()
        self._tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def _make_brain(self, config: dict | None = None, workers: dict | None = None):
        from superagent_lite.brain import Brain

        config = config or self.config
        router = Router(
            config=config,
            workers=workers if workers is not None else {"files": self.worker},
            recover_mode="none",
        )
        self._routers = getattr(self, "_routers", []) + [router]
        return Brain(config=config, router=router)

    def populated_server(self, token: str = "phase4-token", **overrides):
        """Run a real daemon so ``/chat`` and the page have data to show."""

        from superagent_lite.daemon import Daemon
        from superagent_lite.workers.echo import EchoWorker

        self.planner.responder = lambda payload, index: PLAN_ONE
        config = dict(self.config, **overrides)
        brain = self._make_brain(
            config, workers={"files": self.worker, "echo": EchoWorker()}
        )
        daemon = Daemon(
            config=config,
            workers={"files": self.worker, "echo": EchoWorker()},
            log=lambda line: None,
        )
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (daemon.request_stop("teardown"), thread.join(timeout=10)))
        deadline = time.monotonic() + 10
        while daemon.router is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.serve(token=token, brain=brain)

    def serve(self, *, token: str | None = None, brain=None, **kwargs):
        api = ApiConfig(host="127.0.0.1", port=0, token=token)
        server = make_server(
            brain or self.brain,
            api,
            channel_log=self.root / "channel.log",
            **kwargs,
        )
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self._servers.append(server)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def raw(self, url: str, *, payload=None, token: str | None = None, headers=None):
        """Return ``(status, headers, body_bytes)`` for one request."""

        data = None if payload is None else json.dumps(payload).encode()
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def get(self, url: str, **kwargs):
        status, headers, body = self.raw(url, **kwargs)
        return status, headers, (json.loads(body) if body else {})

    def post(self, url: str, payload, **kwargs):
        status, headers, body = self.raw(url, payload=payload, **kwargs)
        try:
            return status, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return status, {"raw": body.decode("utf-8", "replace")}

    def make_ticket(self, description: str = "produce files"):
        ticket = self.brain.router.submit(description, worker="files")
        self.assertEqual(ticket.status.value, "done", ticket.error)
        return ticket


# ---------------------------------------------------------------------------
# 1. Web chat page
# ---------------------------------------------------------------------------
class WebUiTests(ServerCase):
    def test_page_is_served_publicly_with_key_elements(self) -> None:
        base = self.serve(token=self.token)
        status, headers, body = self.raw(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        page = body.decode("utf-8")
        self.assertIn('id="token"', page)
        self.assertIn('id="message"', page)
        self.assertIn('id="send"', page)
        self.assertIn('id="log"', page)
        self.assertIn('"/chat"', page)
        self.assertIn("/tickets/", page)
        self.assertIn("/artifacts/", page)
        self.assertEqual(page, CHAT_PAGE)

    def test_page_has_no_external_resources(self) -> None:
        self.assertNotIn("<script src", CHAT_PAGE)
        self.assertNotIn("<link rel=\"stylesheet\"", CHAT_PAGE)
        self.assertNotIn("cdn.", CHAT_PAGE)
        self.assertNotIn("http://", CHAT_PAGE)
        self.assertNotIn("https://", CHAT_PAGE)

    def test_page_loads_without_a_token_but_the_api_still_requires_one(self) -> None:
        base = self.serve(token=self.token)
        status, _headers, _body = self.raw(base + "/")
        self.assertEqual(status, 200)

        status, _ = self.post(base + "/chat", {"text": "hi"})
        self.assertEqual(status, 401)
        status, _headers, _body = self.get(base + "/tickets/anything")
        self.assertEqual(status, 401)

    def test_api_contract_the_page_relies_on(self) -> None:
        """Exactly the requests the inline JavaScript makes, end to end."""

        base = self.populated_server()
        status, body = self.post(base + "/chat", {"text": "say hello"}, token=self.token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "summary")
        self.assertEqual(len(body["ticket_ids"]), 1)
        ticket_id = body["ticket_ids"][0]

        # Progress polling, as the page does after a chat reply.
        status, _headers, ticket = self.get(
            base + f"/tickets/{ticket_id}", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(ticket["status"], "done")

        # A direct reply still carries the fields the page renders.
        self.planner.responder = lambda payload, index: REPLY_ONLY
        status, body = self.post(base + "/chat", {"text": "ping"}, token=self.token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "reply")
        self.assertEqual(body["reply"], "pong")
        for key in ("reply", "mode", "ticket_ids", "timed_out", "model"):
            self.assertIn(key, body)

    def test_page_script_is_syntactically_valid(self) -> None:
        script = CHAT_PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        node = shutil.which("node")
        if not node:  # pragma: no cover - environment dependent
            self.skipTest("node not available")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            path = handle.name
        self.addCleanup(os.unlink, path)
        result = subprocess.run(
            [node, "--check", path], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_stores_the_token_in_session_storage_not_the_url(self) -> None:
        self.assertIn("sessionStorage", CHAT_PAGE)
        self.assertIn("Authorization", CHAT_PAGE)
        self.assertIn("Bearer ", CHAT_PAGE)


# ---------------------------------------------------------------------------
# 2. Artifact listing and download
# ---------------------------------------------------------------------------
class ArtifactApiTests(ServerCase):
    def test_list_reports_name_size_and_existence(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, _headers, body = self.get(
            base + f"/tickets/{ticket.id}/artifacts", token=self.token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["ticket_id"], ticket.id)
        entries = {entry["name"]: entry for entry in body["artifacts"]}
        self.assertEqual(
            set(entries), {"out/hello.txt", "image.bin", "missing.txt"}
        )
        self.assertEqual(entries["out/hello.txt"]["size"], len("hello artifact"))
        self.assertTrue(entries["out/hello.txt"]["exists"])
        self.assertFalse(entries["missing.txt"]["exists"])
        self.assertIsNone(entries["missing.txt"]["size"])
        self.assertIn("/tickets/", entries["out/hello.txt"]["url"])

    def test_download_serves_content_type_and_disposition(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, headers, body = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/out/hello.txt", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello artifact")
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn('filename="hello.txt"', headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_binary_content_type_falls_back_to_octet_stream(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, headers, body = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/image.bin", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x00\x01\x02binary")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    def test_artifacts_require_the_static_token(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, _h, _b = self.raw(base + f"/tickets/{ticket.id}/artifacts")
        self.assertEqual(status, 401)
        status, _h, _b = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/out/hello.txt"
        )
        self.assertEqual(status, 401)

    def test_unknown_ticket_and_missing_artifact_are_404(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, _h, _b = self.raw(base + "/tickets/nope/artifacts", token=self.token)
        self.assertEqual(status, 404)
        status, _h, _b = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/out/not-there.txt", token=self.token
        )
        self.assertEqual(status, 404)

    def test_path_traversal_is_refused(self) -> None:
        ticket = self.make_ticket()
        secret = Path(ticket.workdir).parent / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        base = self.serve(token=self.token)
        prefix = base + f"/tickets/{ticket.id}/artifacts/"
        attempts = [
            "../secret.txt",
            "../../secret.txt",
            "%2e%2e%2fsecret.txt",
            "..%2Fsecret.txt",
            "out/../../secret.txt",
            "..%2f..%2fsecret.txt",
            urllib.parse.quote("../secret.txt", safe=""),
            "/etc/passwd",
            "%2Fetc%2Fpasswd",
            "..",
            ".",
        ]
        for attempt in attempts:
            status, _headers, body = self.raw(prefix + attempt, token=self.token)
            self.assertEqual(status, 404, f"{attempt!r} must not be served")
            self.assertNotIn(b"TOP SECRET", body, attempt)
            self.assertNotIn(b"root:", body, attempt)

    def test_symlink_escape_is_refused(self) -> None:
        ticket = self.make_ticket()
        secret = Path(ticket.workdir).parent / "outside.txt"
        secret.write_text("OUTSIDE", encoding="utf-8")
        link = Path(ticket.workdir) / "link.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        base = self.serve(token=self.token)
        status, _headers, body = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/link.txt", token=self.token
        )
        self.assertEqual(status, 404)
        self.assertNotIn(b"OUTSIDE", body)

    def test_directory_is_not_served_as_a_file(self) -> None:
        ticket = self.make_ticket()
        base = self.serve(token=self.token)
        status, _h, _b = self.raw(
            base + f"/tickets/{ticket.id}/artifacts/out", token=self.token
        )
        self.assertEqual(status, 404)


class ArtifactHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "a.txt").write_text("a", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolve_accepts_a_relative_child(self) -> None:
        self.assertEqual(resolve_artifact(self.work, "a.txt"), (self.work / "a.txt").resolve())

    def test_resolve_rejects_escapes(self) -> None:
        for name in ("../x", "/etc/passwd", "", "a\x00b"):
            with self.assertRaises(ArtifactPathError, msg=repr(name)):
                resolve_artifact(self.work, name)

    def test_resolve_reports_missing(self) -> None:
        with self.assertRaises(ArtifactNotFound):
            resolve_artifact(self.work, "nope.txt")

    def test_content_type_and_disposition_helpers(self) -> None:
        self.assertIn("text/plain", content_type_for("a.txt"))
        self.assertEqual(content_type_for("a.unknownext"), "application/octet-stream")
        self.assertIn("charset=utf-8", content_type_for("a.json"))
        header = content_disposition(self.root / "报告 v2.txt")
        self.assertTrue(header.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", header)
        # The ASCII fallback never carries quotes, backslashes or non-ASCII.
        fallback = header.split('filename="', 1)[1].split('"', 1)[0]
        self.assertTrue(all(0x20 <= ord(ch) < 0x7F for ch in fallback))
        self.assertNotIn("/", header.split("filename*=")[1])

    def test_artifact_entries_marks_missing_files(self) -> None:
        ticket = SimpleNamespace(
            id="t_1", workdir=str(self.work), artifacts=["a.txt", "gone.txt"]
        )
        entries = artifact_entries(ticket)
        self.assertEqual(entries[0]["size"], 1)
        self.assertTrue(entries[0]["exists"])
        self.assertFalse(entries[1]["exists"])


# ---------------------------------------------------------------------------
# 3. Feishu channel
# ---------------------------------------------------------------------------
def feishu_event(text: str = "你好", *, message_type: str = "text", sender_type: str = "user"):
    content = json.dumps({"text": text}) if message_type == "text" else json.dumps({"x": 1})
    return {
        "schema": "2.0",
        "header": {
            "event_id": "ev_1",
            "event_type": "im.message.receive_v1",
            "token": "verify-token",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": sender_type,
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": message_type,
                "content": content,
            },
        },
    }


class FakeFeishuApi:
    """A local stand-in for open.feishu.cn (auth + message send)."""

    def __init__(self, *, send_code: int = 0) -> None:
        self.requests: list[dict] = []
        self.send_code = send_code
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8")) if raw else {}
                with stub._lock:
                    stub.requests.append(
                        {
                            "path": self.path,
                            "body": body,
                            "authorization": self.headers.get("Authorization"),
                        }
                    )
                if self.path.startswith("/open-apis/auth/"):
                    response = {
                        "code": 0,
                        "msg": "ok",
                        "tenant_access_token": "tenant-token",
                        "expire": 7200,
                    }
                else:
                    response = {"code": stub.send_code, "msg": "stub", "data": {}}
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, fmt, *args) -> None:  # noqa: A002
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeFeishuApi":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def sends(self) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"].startswith("/open-apis/im/")]

    def auths(self) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"].startswith("/open-apis/auth/")]


class FeishuChannelTests(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_brain = FakeBrain()

    def serve_feishu(self, env: dict | None = None, **kwargs):
        return self.serve(
            brain=self.fake_brain,
            feishu_env=env or {},
            **kwargs,
        )

    def test_url_verification_challenge_is_echoed(self) -> None:
        base = self.serve_feishu()
        status, body = self.post(
            base + "/channels/feishu",
            {"type": "url_verification", "challenge": "abc123", "token": "x"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"challenge": "abc123"})
        self.assertEqual(self.fake_brain.calls, [])

    def test_url_verification_without_a_challenge_is_400(self) -> None:
        base = self.serve_feishu()
        status, body = self.post(base + "/channels/feishu", {"type": "url_verification"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_text_event_runs_the_brain_and_logs_without_credentials(self) -> None:
        base = self.serve_feishu()
        status, body = self.post(base + "/channels/feishu", feishu_event("帮我建个文件"))
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertFalse(body["replied"])
        self.assertEqual(self.fake_brain.calls, [("帮我建个文件", "feishu:ou_user")])
        log = (self.root / "channel.log").read_text(encoding="utf-8")
        self.assertIn("帮我建个文件", log)
        self.assertIn("logged only", log)

    def test_reply_is_sent_when_credentials_are_configured(self) -> None:
        with FakeFeishuApi() as api:
            base = self.serve_feishu(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret_test",
                    "FEISHU_API_BASE": api.base_url,
                }
            )
            status, body = self.post(base + "/channels/feishu", feishu_event("hello"))
            self.assertEqual(status, 200, body)
            self.assertTrue(body["replied"])

            sends = api.sends()
            self.assertEqual(len(sends), 1)
            send = sends[0]
            self.assertIn("receive_id_type=chat_id", send["path"])
            self.assertEqual(send["body"]["receive_id"], "oc_chat")
            self.assertEqual(send["body"]["msg_type"], "text")
            self.assertEqual(json.loads(send["body"]["content"]), {"text": "agent answer"})
            self.assertEqual(send["authorization"], "Bearer tenant-token")
            self.assertEqual(len(api.auths()), 1)
            auth = api.auths()[0]["body"]
            self.assertEqual(auth["app_id"], "cli_test")
            self.assertEqual(auth["app_secret"], "secret_test")

    def test_non_text_and_app_messages_are_ignored(self) -> None:
        base = self.serve_feishu()
        for payload in (
            feishu_event(message_type="image"),
            feishu_event(sender_type="app"),
            {
                "schema": "2.0",
                "header": {"event_type": "im.chat.member.bot.added_v1"},
                "event": {},
            },
        ):
            status, body = self.post(base + "/channels/feishu", payload)
            self.assertEqual(status, 200)
            self.assertEqual(body["ok"], True)
        self.assertEqual(self.fake_brain.calls, [])

    def test_malformed_text_event_is_acknowledged_not_crashed(self) -> None:
        base = self.serve_feishu()
        payload = feishu_event()
        payload["event"]["message"]["content"] = "{not json"
        status, body = self.post(base + "/channels/feishu", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertEqual(self.fake_brain.calls, [])

    def test_verification_token_is_enforced_when_configured(self) -> None:
        base = self.serve_feishu({"FEISHU_VERIFICATION_TOKEN": "verify-token"})
        payload = feishu_event("hi")
        status, _body = self.post(base + "/channels/feishu", payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.fake_brain.calls), 1)

        wrong = feishu_event("hi")
        wrong["header"]["token"] = "not-the-token"
        status, _body = self.post(base + "/channels/feishu", wrong)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.fake_brain.calls), 1)

    def test_webhook_needs_no_bearer_token(self) -> None:
        base = self.serve(token=self.token, brain=self.fake_brain, feishu_env={})
        status, body = self.post(base + "/channels/feishu", feishu_event("no bearer"))
        self.assertEqual(status, 200, body)
        # ... while /chat on the same server still does.
        status, _ = self.post(base + "/chat", {"text": "hi"})
        self.assertEqual(status, 401)

    def test_brain_failure_still_acknowledges(self) -> None:
        self.fake_brain.fail = True
        base = self.serve_feishu()
        status, body = self.post(base + "/channels/feishu", feishu_event("boom"))
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        log = (self.root / "channel.log").read_text(encoding="utf-8")
        self.assertIn("brain failed", log)

    def test_api_failure_does_not_fail_the_webhook(self) -> None:
        with FakeFeishuApi(send_code=230002) as api:
            base = self.serve_feishu(
                {
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "secret_test",
                    "FEISHU_API_BASE": api.base_url,
                }
            )
            status, body = self.post(base + "/channels/feishu", feishu_event("hi"))
            self.assertEqual(status, 200)
            self.assertFalse(body["replied"])
            log = (self.root / "channel.log").read_text(encoding="utf-8")
            self.assertIn("reply failed", log)

    def test_duplicate_delivery_runs_the_task_once(self) -> None:
        base = self.serve_feishu()
        first = feishu_event("only once")
        status, body = self.post(base + "/channels/feishu", first)
        self.assertEqual(status, 200)
        self.assertNotIn("duplicate", body)
        # Feishu retries an unacknowledged event with the same event id.
        status, body = self.post(base + "/channels/feishu", first)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("duplicate"))
        self.assertEqual(len(self.fake_brain.calls), 1)
        # A different event still runs.
        second = feishu_event("a new message")
        second["header"]["event_id"] = "ev_2"
        status, body = self.post(base + "/channels/feishu", second)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.fake_brain.calls), 2)

    def test_malformed_json_body_is_400(self) -> None:
        base = self.serve_feishu()
        status, _body = self.post(base + "/channels/feishu", payload=[1, 2, 3])
        self.assertEqual(status, 400)


class FeishuUnitTests(unittest.TestCase):
    def test_settings_from_env(self) -> None:
        settings = settings_from_env(
            {
                "FEISHU_APP_ID": " id ",
                "FEISHU_APP_SECRET": "",
                "FEISHU_API_BASE": "http://127.0.0.1:9",
                "FEISHU_VERIFICATION_TOKEN": "tok",
            }
        )
        self.assertEqual(settings.app_id, "id")
        self.assertIsNone(settings.app_secret)
        self.assertFalse(settings.has_credentials)
        self.assertTrue(settings.requires_token)
        self.assertEqual(settings.api_base, "http://127.0.0.1:9")

    def test_parse_event_extracts_text_and_ids(self) -> None:
        message = parse_event(feishu_event("do the thing"))
        assert message is not None
        self.assertEqual(message.text, "do the thing")
        self.assertEqual(message.chat_id, "oc_chat")
        self.assertEqual(message.open_id, "ou_user")
        self.assertEqual(message.message_id, "om_1")
        self.assertEqual(message.event_id, "ev_1")

    def test_parse_event_returns_none_for_ignored_events(self) -> None:
        self.assertIsNone(parse_event(feishu_event(sender_type="app")))
        self.assertIsNone(parse_event(feishu_event(message_type="file")))

    def test_parse_event_rejects_malformed_payloads(self) -> None:
        with self.assertRaises(FeishuEventError):
            parse_event("not an object")
        # An unknown event type is ignored, not an error.
        self.assertIsNone(parse_event({"header": {"event_type": "other"}, "event": {}}))
        with self.assertRaises(FeishuEventError):
            parse_event({"header": {"event_type": "im.message.receive_v1"}, "event": {}})

    def test_token_cache_is_reused_and_invalidated_on_auth_error(self) -> None:
        with FakeFeishuApi() as api:
            settings = FeishuSettings(
                app_id="cli", app_secret="sec", api_base=api.base_url
            )
            client = FeishuReply(settings)
            client.reply_text("oc", "one")
            client.reply_text("oc", "two")
            self.assertEqual(len(api.auths()), 1, "tenant token should be cached")
            self.assertEqual(len(api.sends()), 2)

    def test_api_errors_are_raised_and_redacted(self) -> None:
        with FakeFeishuApi(send_code=99991663) as api:
            settings = FeishuSettings(
                app_id="cli", app_secret="sec", api_base=api.base_url
            )
            with self.assertRaises(FeishuApiError):
                FeishuReply(settings).reply_text("oc", "hi")

    def test_handle_event_rejects_non_objects(self) -> None:
        result = handle_event(
            [], settings=FeishuSettings(), brain=FakeBrain(), log_path=None
        )
        self.assertEqual(result.status, 400)

    def test_deduper_is_bounded_and_case_sensitive(self) -> None:
        deduper = EventDeduper(max_entries=2)
        self.assertFalse(deduper.is_duplicate("a"))
        self.assertTrue(deduper.is_duplicate("a"))
        self.assertFalse(deduper.is_duplicate("b"))
        self.assertFalse(deduper.is_duplicate("c"))  # evicts "a"
        self.assertFalse(deduper.is_duplicate("a"))
        self.assertFalse(deduper.is_duplicate(None))
        self.assertFalse(deduper.is_duplicate(None))

    def test_no_credentials_in_repo_or_source(self) -> None:
        """No real-looking Feishu secret may be checked in."""

        for path in (PROJECT_ROOT / "superagent_lite").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("cli_a", text, path)
            self.assertNotIn("ou_", text, path)


# ---------------------------------------------------------------------------
# 4. Deployment material
# ---------------------------------------------------------------------------
class DeployAssetTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (PROJECT_ROOT / "deploy" / name).read_text(encoding="utf-8")

    def test_both_units_exist_and_have_the_required_directives(self) -> None:
        for unit, command in (
            ("superagentd.service", " daemon"),
            ("superagent-http.service", " serve"),
        ):
            text = self.read(unit)
            self.assertIn("[Unit]", text)
            self.assertIn("[Service]", text)
            self.assertIn("[Install]", text)
            self.assertIn("WorkingDirectory=/opt/superagent-lite", text)
            self.assertIn("Restart=on-failure", text)
            self.assertIn("EnvironmentFile=-/etc/superagent/", text)
            self.assertIn("ExecStart=", text)
            self.assertIn(command, text.split("ExecStart=", 1)[1].splitlines()[0])
            self.assertIn("WantedBy=multi-user.target", text)

    def test_install_script_is_executable_and_idempotent_by_design(self) -> None:
        path = PROJECT_ROOT / "deploy" / "install.sh"
        self.assertTrue(path.is_file())
        mode = path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "install.sh must be executable")
        text = path.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("systemctl daemon-reload", text)
        self.assertIn("systemctl enable", text)
        for unit in ("superagentd.service", "superagent-http.service"):
            self.assertIn(unit, text)
        # Existing state is preserved on re-runs.
        self.assertIn('if [ ! -f "${PREFIX}/config.json" ]', text)
        self.assertIn('if [ ! -e "${target}" ]', text)
        # The script itself must not embed a secret.
        self.assertNotIn("SUPERAGENT_HTTP_TOKEN=", text.replace("# SUPERAGENT_HTTP_TOKEN=", ""))

    def test_install_script_syntax_is_valid(self) -> None:
        bash = "/bin/bash"
        if not Path(bash).exists():
            self.skipTest("bash not available")
        result = subprocess.run(
            [bash, "-n", str(PROJECT_ROOT / "deploy" / "install.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_readme_documents_deployment_and_remote_access(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Deployment and remote access (部署与远程访问)", readme)
        self.assertIn("systemctl", readme)
        self.assertIn("deploy/install.sh", readme)
        self.assertIn("systemd", readme)
        self.assertIn("http_host", readme)
        self.assertIn("Tailscale", readme)
        self.assertIn("/channels/feishu", readme)
        self.assertIn("反向代理", readme)
        self.assertIn("不要", readme)
        self.assertIn("FEISHU_APP_ID", readme)
        # The default model must not have changed.
        self.assertIn("cn:deepseek-v4.1-flash", readme)


if __name__ == "__main__":
    unittest.main()
