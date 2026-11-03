"""Test helpers: a stub ``claude`` executable and small fixture builders."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

STUB_SCRIPT = r'''#!/usr/bin/env python3
"""Stub Claude Code CLI used by the tests.

Behaviour comes from stub_state.json next to this script; the argv and the
(child) environment it was given are recorded in stub_record.json.
"""

import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
state_path = here / "stub_state.json"
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

record = {
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": dict(os.environ),
}
(here / "stub_record.json").write_text(json.dumps(record), encoding="utf-8")


def track(event):
    """Append one line to the shared tracker file, if the test asked for it."""
    target = state.get("track")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(event + "\n")
        handle.flush()


extra = state.get("stdout")
if extra:
    print(extra)

mode = state.get("mode", "success")
if mode == "sleep":
    track("start " + os.getcwd())
    time.sleep(float(state.get("sleep", 1.0)))
    track("end " + os.getcwd())
    Path("answer.txt").write_text(state.get("content", "chain-ok"), encoding="utf-8")
    print("Wrote answer.txt into the working directory.")
    print('RESULT {"status": "success", "summary": "created answer.txt", '
          '"artifacts": ["answer.txt"]}')
elif mode == "success":
    Path("answer.txt").write_text(state.get("content", "chain-ok"), encoding="utf-8")
    print("Wrote answer.txt into the working directory.")
    print('RESULT {"status": "success", "summary": "created answer.txt", '
          '"artifacts": ["answer.txt"]}')
elif mode == "failed":
    print('RESULT {"status": "failed", "summary": "cannot write outside the '
          'working directory", "artifacts": []}')
elif mode == "missing":
    print("I am sorry, but I need you to grant me permission first.")
elif mode == "garbage":
    print("RESULT {this is not json}")
elif mode == "wrong_status":
    print('RESULT {"status": "ok", "summary": "fine", "artifacts": []}')
elif mode == "nonzero":
    print("fatal: something broke", file=sys.stderr)
    sys.exit(3)
elif mode == "escape":
    target = state.get("target", "../escaped.txt")
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text("escaped", encoding="utf-8")
    except OSError as exc:
        print('RESULT {"status": "failed", "summary": "cannot write '
              + target + ': ' + str(exc) + '", "artifacts": []}')
        sys.exit(0)
    print('RESULT {"status": "success", "summary": "wrote outside", '
          '"artifacts": ["' + target + '"]}')
else:
    raise SystemExit("unknown stub mode: " + mode)

sys.exit(int(state.get("exit_code", 0)))
'''

STUB_NAME = "claude"


def make_stub_claude(
    directory: str | Path,
    mode: str = "success",
    name: str = STUB_NAME,
    **state,
) -> Path:
    """Create an executable stub ``claude`` in ``directory``.

    ``mode`` and extra keyword arguments (``content``, ``stdout``,
    ``exit_code``, ``target``) are written to ``stub_state.json`` next to it.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(STUB_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    set_stub_state(directory, mode=mode, **state)
    return script


def set_stub_state(directory: str | Path, **state) -> None:
    path = Path(directory) / "stub_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")


def stub_record(directory: str | Path) -> dict:
    path = Path(directory) / "stub_record.json"
    return json.loads(path.read_text(encoding="utf-8"))


def python_executable() -> str:
    return sys.executable or "python3"


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02):
    """Poll ``predicate`` until it returns a truthy value or ``timeout``."""

    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        _time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout:g}s: {predicate}")


# ---------------------------------------------------------------------------
# Stub Anthropic-compatible planner endpoint
# ---------------------------------------------------------------------------
class StubPlanner:
    """A local ``/v1/messages`` server the Brain can be pointed at.

    No network, no credentials: the Brain resolves the endpoint from a
    ``settings.json`` written by :meth:`settings_file`, exactly like it does
    with ``~/.claude/settings.json`` in production.

    ``responder(payload, index)`` returns the assistant text, or raises to
    simulate a model-side crash.  Requests are recorded (headers + JSON body)
    for assertions.
    """

    def __init__(self, *, token: str = "test-planner-token", responder=None) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        def _authorized(headers, token: str) -> bool:
            supplied = headers.get("Authorization") or headers.get("X-Api-Key") or ""
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:]
            return supplied.strip() == token

        stub = self
        self.token = token
        self.requests: list[dict] = []
        self.responder = responder or (
            lambda payload, index: '{"action": "reply", "reply": "ok"}'
        )
        self._lock = threading.Lock()
        self.status = 200
        self.error_body = ""

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                import json as _json

                if self.path != "/v1/messages":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                try:
                    payload = _json.loads(body.decode("utf-8"))
                except Exception:
                    payload = {"unparsed": body.decode("utf-8", "replace")}
                with stub._lock:
                    index = len(stub.requests)
                    stub.requests.append(
                        {
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                            "payload": payload,
                        }
                    )
                    status = stub.status
                    error_body = stub.error_body
                if stub.token and not _authorized(self.headers, stub.token):
                    self._respond(401, _json.dumps({"error": "unauthorized"}))
                    return
                if status != 200:
                    self._respond(status, error_body or '{"error": "stub failure"}')
                    return
                text = stub.responder(payload, index)
                answer = _json.dumps(
                    {
                        "id": f"msg_{index}",
                        "type": "message",
                        "role": "assistant",
                        "model": payload.get("model", "stub"),
                        "content": [
                            {"type": "thinking", "thinking": "internal reasoning"},
                            {"type": "text", "text": text},
                        ],
                        "stop_reason": "end_turn",
                    }
                )
                self._respond(200, answer)

            def _respond(self, code: int, text: str) -> None:
                raw = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args) -> None:  # noqa: A002
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="stub-planner", daemon=True
        )

    # -- lifecycle ------------------------------------------------------
    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "StubPlanner":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "StubPlanner":
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    # -- helpers --------------------------------------------------------
    def settings_file(self, path: str | Path) -> Path:
        """Write a Claude-Code-shaped settings file pointing at this stub."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": self.base_url,
                        "ANTHROPIC_AUTH_TOKEN": self.token,
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def payloads(self) -> list[dict]:
        with self._lock:
            return [entry["payload"] for entry in self.requests]

    def headers(self) -> list[dict]:
        with self._lock:
            return [entry["headers"] for entry in self.requests]
