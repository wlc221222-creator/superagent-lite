"""A minimal HTTP API for the main agent, standard library only.

Endpoints
---------
``POST /chat``      ``{"text": "...", "user_id": "..."}`` -> the main agent's
                    reply.  Waits for the request's tickets up to
                    ``chat_timeout_seconds``; on timeout it answers with
                    ``mode: "pending"`` and the ticket ids instead.
``GET  /tickets/<id>``  one ticket's state and structured result.
``GET  /health``    liveness plus queue counters (unauthenticated).

Auth
----
When the environment variable ``SUPERAGENT_HTTP_TOKEN`` is set, every request
except ``GET /health`` must carry ``Authorization: Bearer <token>`` or
``X-SuperAgent-Token: <token>``; anything else is rejected with 401 before the
body is read.  With the variable unset the server is open (the default bind
address is ``127.0.0.1``, but binding to ``0.0.0.0`` without a token is a
configuration mistake the CLI warns about).

Request handling runs on :class:`http.server.ThreadingHTTPServer`, so one slow
chat does not block ``GET /tickets`` from another client.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

from .brain import Brain
from .models import Ticket, TicketStatus

TOKEN_ENV = "SUPERAGENT_HTTP_TOKEN"
DEFAULT_MAX_BODY = 64 * 1024

_TICKET_PATH = re.compile(r"^/tickets/(?P<id>[A-Za-z0-9_.-]+)/?$")


def token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured static token, or ``None`` when auth is off."""

    source = os.environ if environ is None else environ
    token = source.get(TOKEN_ENV)
    return token.strip() if token and token.strip() else None


@dataclass(slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY


class _Server(ThreadingHTTPServer):
    """Carries the shared Brain and the auth token for every request thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], brain: Brain, api: ApiConfig) -> None:
        super().__init__(address, _Handler)
        self.brain = brain
        self.api = api


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "superagent-lite"
    sys_version = ""

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "auth": "token" if self.server.api.token else "open",
                    "queue": _queue_counts(self.server.brain),
                },
            )
            return
        if not self._authorized():
            return
        match = _TICKET_PATH.match(path)
        if match:
            ticket = self.server.brain.router.get(match.group("id"))
            if ticket is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"error": f"ticket not found: {match.group('id')}"}
                )
                return
            self._send_json(HTTPStatus.OK, ticket_dict(ticket))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        path = urlsplit(self.path).path
        if path not in ("/chat", "/chat/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        payload = self._read_json()
        if payload is None:
            return

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "field 'text' must be a non-empty string"}
            )
            return
        user_id = payload.get("user_id", "http")
        if not isinstance(user_id, str) or not user_id.strip():
            user_id = "http"

        result = self.server.brain.reply(text, user_id=user_id)
        status = (
            HTTPStatus.BAD_GATEWAY
            if result.mode == "error"
            else HTTPStatus.OK
        )
        self._send_json(status, result.to_dict())

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- helpers --------------------------------------------------------
    def _authorized(self) -> bool:
        expected = self.server.api.token
        if not expected:
            return True
        supplied = _bearer(self.headers.get("Authorization")) or (
            self.headers.get("X-SuperAgent-Token") or ""
        ).strip()
        if supplied and hmac.compare_digest(supplied, expected):
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "unauthorized"},
        )
        return False

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body required"})
            return None
        if length > self.server.api.max_body_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body too large"}
            )
            return None
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be JSON"})
            return None
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
            return None
        return data

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Default logging writes every request to stderr; keep it but drop the
        # peer address, which is noise for a chat endpoint, and never log a
        # request body (it is user text, and could carry a pasted secret).
        if os.environ.get("SUPERAGENT_HTTP_QUIET"):
            return
        super().log_message(format, *args)


def _bearer(header: str | None) -> str:
    if not header:
        return ""
    value = header.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _queue_counts(brain: Brain) -> dict[str, int]:
    store = brain.router.store
    return {
        status.value: store.count(status)
        for status in (
            TicketStatus.PENDING,
            TicketStatus.RUNNING,
            TicketStatus.DONE,
            TicketStatus.FAILED,
        )
    }


def ticket_dict(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "description": ticket.description,
        "status": ticket.status.value,
        "worker": ticket.worker,
        "workdir": ticket.workdir,
        "depends_on": list(ticket.depends_on),
        "result_summary": ticket.result_summary,
        "artifacts": list(ticket.artifacts),
        "error": ticket.error,
        "exit_code": ticket.exit_code,
        "attempts": ticket.attempts,
        "retry_count": ticket.retry_count,
        "max_retries": ticket.max_retries,
        "created_at": ticket.created_at,
        "started_at": ticket.started_at,
        "heartbeat_at": ticket.heartbeat_at,
        "finished_at": ticket.finished_at,
    }


def make_server(brain: Brain, api: ApiConfig) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server."""

    return _Server((api.host, api.port), brain, api)


def serve(brain: Brain, api: ApiConfig, *, ready: Any = None) -> None:
    """Run the server until ``KeyboardInterrupt``."""

    server = make_server(brain, api)
    try:
        if ready is not None:
            ready(server)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    finally:
        server.server_close()
