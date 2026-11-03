"""A minimal HTTP API for the main agent, standard library only.

Endpoints
---------
``GET  /``           the single-file web chat page (public; talks to the API
                     with the static token).
``POST /chat``      ``{"text": "...", "user_id": "..."}`` -> the main agent's
                    reply.  Waits for the request's tickets up to
                    ``chat_timeout_seconds``; on timeout it answers with
                    ``mode: "pending"`` and the ticket ids instead.
``GET  /tickets/<id>``  one ticket's state and structured result.
``GET  /tickets/<id>/artifacts``  ``{"ticket_id", "artifacts": [...]}`` -- the
                    files the ticket recorded, with size and URL.
``GET  /tickets/<id>/artifacts/<name>``  the file itself, with a guessed
                    ``Content-Type`` and a ``Content-Disposition``.  Only
                    regular files inside the ticket's workdir are served;
                    ``../`` and symlink escapes are 404.
``POST /tickets/<id>/approve`` / ``POST /tickets/<id>/reject``  the
                    approval gate's control path (token auth, like the rest).
``GET  /attention``  the read-only "needs attention" list: tickets awaiting
                    approval and tickets whose semantic review was
                    inconclusive.  Display only -- approving stays on the
                    CLI/HTTP control path.
``POST /channels/feishu``  Feishu event subscription webhook (see
                    :mod:`superagent_lite.channels`).  It authenticates with
                    Feishu's own verification token, not the static bearer
                    token, because Feishu cannot be given custom headers.
``GET  /health``    liveness plus queue counters (unauthenticated).

Auth
----
When the environment variable ``SUPERAGENT_HTTP_TOKEN`` is set, every request
except ``GET /health``, ``GET /`` and the Feishu webhook must carry
``Authorization: Bearer <token>`` or ``X-SuperAgent-Token: <token>``; anything
else is rejected with 401 before the body is read.  With the variable unset
the server is open (the default bind address is ``127.0.0.1``, but binding to
``0.0.0.0`` without a token is a configuration mistake the CLI warns about).

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
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from .artifacts import (
    ArtifactError,
    artifact_entries,
    content_disposition,
    content_type_for,
    resolve_artifact,
)
from .brain import Brain
from .channels import EventDeduper, FeishuReply, handle_event, settings_from_env
from .models import Ticket, TicketStatus
from .webui import CHAT_PAGE

TOKEN_ENV = "SUPERAGENT_HTTP_TOKEN"
DEFAULT_MAX_BODY = 64 * 1024
#: Feishu event payloads can carry quoted text; give the webhook some room
#: while still refusing anything that looks like an upload.
DEFAULT_MAX_CHANNEL_BODY = 256 * 1024
CHUNK_SIZE = 64 * 1024

_TICKET_ID = r"(?P<id>[A-Za-z0-9_.-]+)"
_TICKET_PATH = re.compile(rf"^/tickets/{_TICKET_ID}/?$")
_ARTIFACTS_INDEX = re.compile(rf"^/tickets/{_TICKET_ID}/artifacts/?$")
_ARTIFACT_FILE = re.compile(rf"^/tickets/{_TICKET_ID}/artifacts/(?P<name>.+)$")
_APPROVE_PATH = re.compile(rf"^/tickets/{_TICKET_ID}/approve/?$")
_REJECT_PATH = re.compile(rf"^/tickets/{_TICKET_ID}/reject/?$")

FEISHU_PATH = "/channels/feishu"



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
    max_channel_body_bytes: int = DEFAULT_MAX_CHANNEL_BODY


class _Server(ThreadingHTTPServer):
    """Carries the shared Brain and the auth token for every request thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        brain: Brain,
        api: ApiConfig,
        *,
        feishu_env: Mapping[str, str] | None = None,
        channel_log: str | os.PathLike[str] | None = None,
        feishu_client: FeishuReply | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.brain = brain
        self.api = api
        # Read at request time so a restart is not needed after exporting the
        # credentials; tests inject a mapping instead of touching os.environ.
        self.feishu_env = feishu_env
        self.channel_log = channel_log
        self.feishu_client = feishu_client
        self.feishu_deduper = EventDeduper()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "superagent-lite"
    sys_version = ""

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            # Public on purpose: a browser must be able to load the page to
            # type a token into it.  Every API call it makes is guarded.
            self._send_html(HTTPStatus.OK, CHAT_PAGE)
            return
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

        if path in ("/attention", "/attention/"):
            tickets = self.server.brain.router.list(status=None, limit=500)
            self._send_json(
                HTTPStatus.OK, {"tickets": needs_attention(tickets)}
            )
            return
        match = _ARTIFACT_FILE.match(path)
        if match:
            self._send_artifact(match.group("id"), match.group("name"))
            return
        match = _ARTIFACTS_INDEX.match(path)
        if match:
            self._list_artifacts(match.group("id"))
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
        if path in (FEISHU_PATH, FEISHU_PATH + "/"):
            self._handle_feishu()
            return
        approve = _APPROVE_PATH.match(path)
        reject = _REJECT_PATH.match(path)
        if approve or reject:
            if not self._authorized():
                return
            self._handle_approval(
                (approve or reject).group("id"), approve=bool(approve)
            )
            return
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

    # -- approval gate --------------------------------------------------
    def _handle_approval(self, ticket_id: str, *, approve: bool) -> None:
        """``POST /tickets/<id>/approve|reject`` (the CLI's control path).

        The request body is ignored; the transition itself is the command.
        A ticket that is not ``awaiting_approval`` is a 409, an unknown id a
        404 -- both with a plain ``error`` string, like every other route.
        """

        brain = self.server.brain
        router = brain.router
        if router.get(ticket_id) is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": f"ticket not found: {ticket_id}"}
            )
            return
        if approve:
            ticket, error = router.approve(ticket_id)
        else:
            ticket, error = router.reject(ticket_id)
        if ticket is None:
            self._send_json(HTTPStatus.CONFLICT, {"error": error})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "ticket": ticket_dict(ticket)},
        )

    # -- artifacts ------------------------------------------------------
    def _list_artifacts(self, ticket_id: str) -> None:
        ticket = self.server.brain.router.get(ticket_id)
        if ticket is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": f"ticket not found: {ticket_id}"}
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"ticket_id": ticket.id, "artifacts": artifact_entries(ticket)},
        )

    def _send_artifact(self, ticket_id: str, raw_name: str) -> None:
        ticket = self.server.brain.router.get(ticket_id)
        if ticket is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": f"ticket not found: {ticket_id}"}
            )
            return
        name = unquote(raw_name)
        try:
            path = resolve_artifact(ticket.workdir, name)
        except ArtifactError as exc:
            # Deliberately the same 404 for "escapes the workdir" and "does
            # not exist": a client has no business learning which names are
            # real outside the directory.
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return

        try:
            size = path.stat().st_size
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "artifact not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type_for(path))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", content_disposition(path))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up mid-download; nothing left to do.
            return

    # -- Feishu channel -------------------------------------------------
    def _handle_feishu(self) -> None:
        env = self.server.feishu_env
        settings = settings_from_env(env)
        payload = self._read_json(max_bytes=self.server.api.max_channel_body_bytes)
        if payload is None:
            return
        channel_log = self.server.channel_log or os.environ.get(
            "SUPERAGENT_CHANNEL_LOG", "runtime/channel.log"
        )
        result = handle_event(
            payload,
            settings=settings,
            brain=self.server.brain,
            reply_client=self.server.feishu_client,
            log_path=channel_log,
            env=env,
            deduper=self.server.feishu_deduper,
        )
        self._send_json(HTTPStatus(result.status), result.body)

    def _read_json(self, *, max_bytes: int | None = None) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body required"})
            return None
        limit = self.server.api.max_body_bytes if max_bytes is None else max_bytes
        if length > limit:
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

    def _send_html(self, status: HTTPStatus, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
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
            TicketStatus.AWAITING_APPROVAL,
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
        "origin": ticket.origin,
        "workdir": ticket.workdir,
        "depends_on": list(ticket.depends_on),
        "result_summary": ticket.result_summary,
        "artifacts": list(ticket.artifacts),
        "error": ticket.error,
        "exit_code": ticket.exit_code,
        "attempts": ticket.attempts,
        "retry_count": ticket.retry_count,
        "max_retries": ticket.max_retries,
        "review_status": ticket.review_status,
        "review_reasons": list(ticket.review_reasons),
        "review_attempts": ticket.review_attempts,
        "created_at": ticket.created_at,
        "started_at": ticket.started_at,
        "heartbeat_at": ticket.heartbeat_at,
        "finished_at": ticket.finished_at,
    }


def needs_attention(tickets: Iterable[Ticket]) -> list[dict[str, Any]]:
    """Rows for the read-only "needs attention" list (Revision 4).

    Awaiting-approval tickets plus inconclusive reviews: both are states a
    human should look at, and neither is a new gate -- the list is display
    only.  Approval actions stay on the CLI/HTTP control path.
    """

    attention: list[dict[str, Any]] = []
    for ticket in tickets:
        reason = None
        if ticket.awaits_approval:
            reason = "awaiting_approval"
        elif ticket.review_status == "inconclusive":
            reason = "review_inconclusive"
        if reason is None:
            continue
        attention.append(
            {
                "id": ticket.id,
                "status": ticket.status.value,
                "reason": reason,
                "description": ticket.description,
                "review_status": ticket.review_status,
                "created_at": ticket.created_at,
            }
        )
    return attention


def make_server(
    brain: Brain,
    api: ApiConfig,
    *,
    feishu_env: Mapping[str, str] | None = None,
    channel_log: str | os.PathLike[str] | None = None,
    feishu_client: FeishuReply | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server.

    ``feishu_env`` overrides the process environment for the Feishu channel
    (tests use this; production leaves it ``None`` so credentials exported by
    systemd's ``EnvironmentFile`` are read at request time).
    """

    return _Server(
        (api.host, api.port),
        brain,
        api,
        feishu_env=feishu_env,
        channel_log=channel_log,
        feishu_client=feishu_client,
    )


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
