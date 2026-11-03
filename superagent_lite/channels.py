"""Feishu (Lark) messaging channel -- skeleton, standard library only.

``POST /channels/feishu`` accepts Feishu's event subscription payloads:

* **url_verification** -- the setup handshake: echo ``{"challenge": ...}``
  back (Feishu expects exactly that object).
* **im.message.receive_v1** -- a user message.  The text is pulled out of the
  ``content`` JSON (``{"text": "..."}``) and handed to the same
  :class:`~superagent_lite.brain.Brain` the web UI uses.  The reply is sent
  back through the Feishu API when app credentials are configured, otherwise
  it is only written to the log.

Credentials are **never** stored in this repository or in ``config.json``.
They are read from the environment at call time:

``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET``   -- the app credentials
``FEISHU_API_BASE``                          -- optional override (default
                                                ``https://open.feishu.cn``);
                                                tests point it at a local stub.

Other Feishu events, non-text messages and messages sent by a bot are
acknowledged with 200 and ignored, which is what Feishu's retry logic expects
(anything else makes it re-deliver the event).

Signature verification: when ``FEISHU_VERIFICATION_TOKEN`` is set, the
``token`` field Feishu puts in every event must match, otherwise the request
is refused with 401.  When it is unset (the skeleton's default) any payload
is accepted -- do not expose the endpoint publicly in that state.

Note: Feishu's newer *signed requests* scheme (``X-Lark-Signature``, a
SHA-256 of timestamp + nonce + encrypt-key + body) protects the body with the
app's encrypt key.  It is **not** implemented here; if the app enables
encryption, put the endpoint behind a reverse proxy / network you control.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from .logs import append_log
from .redact import sanitize_text

APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
API_BASE_ENV = "FEISHU_API_BASE"
VERIFICATION_TOKEN_ENV = "FEISHU_VERIFICATION_TOKEN"
DEFAULT_API_BASE = "https://open.feishu.cn"

#: Tenant access tokens are cached per app id and refreshed this many seconds
#: before Feishu says they expire.
_TOKEN_SKEW_SECONDS = 120
_HTTP_TIMEOUT_SECONDS = 10

TEXT_MESSAGE = "text"


@dataclass(slots=True)
class FeishuSettings:
    """How to talk to Feishu; empty credentials mean "log only"."""

    app_id: str | None = None
    app_secret: str | None = None
    api_base: str = DEFAULT_API_BASE
    verification_token: str | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def requires_token(self) -> bool:
        return bool(self.verification_token)


def settings_from_env(environ: Mapping[str, str] | None = None) -> FeishuSettings:
    source = os.environ if environ is None else environ

    def value(name: str) -> str | None:
        raw = source.get(name)
        return raw.strip() if raw and raw.strip() else None

    return FeishuSettings(
        app_id=value(APP_ID_ENV),
        app_secret=value(APP_SECRET_ENV),
        api_base=value(API_BASE_ENV) or DEFAULT_API_BASE,
        verification_token=value(VERIFICATION_TOKEN_ENV),
    )


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FeishuMessage:
    """The slice of ``im.message.receive_v1`` the channel acts on."""

    text: str
    chat_id: str
    open_id: str | None = None
    message_id: str | None = None
    chat_type: str | None = None
    event_id: str | None = None


class FeishuEventError(ValueError):
    """The payload is not a usable Feishu event."""


def parse_event(payload: Any) -> FeishuMessage | None:
    """Extract a text message from a Feishu event payload.

    Returns ``None`` for events the channel ignores (not a user message, not
    text, sent by a bot).  Raises :class:`FeishuEventError` only when the
    payload claims to be a text message but is malformed.
    """

    if not isinstance(payload, dict):
        raise FeishuEventError("event payload must be a JSON object")

    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise FeishuEventError("event payload is missing 'header' or 'event'")

    if header.get("event_type") != "im.message.receive_v1":
        return None

    sender = event.get("sender") or {}
    sender_type = sender.get("sender_type") if isinstance(sender, dict) else None
    if sender_type == "app":
        # Our own replies arrive as app messages; never answer them.
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        raise FeishuEventError("event is missing 'message'")
    if message.get("message_type") != TEXT_MESSAGE:
        return None

    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise FeishuEventError(f"message content is not valid JSON: {exc}") from exc
    if not isinstance(content, dict):
        raise FeishuEventError("message content must be a JSON object")

    text = content.get("text")
    if not isinstance(text, str) or not text.strip():
        raise FeishuEventError("message content has no text")

    chat_id = message.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise FeishuEventError("message has no chat_id")

    open_id = None
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id") or {}
        if isinstance(sender_id, dict) and isinstance(sender_id.get("open_id"), str):
            open_id = sender_id["open_id"]

    return FeishuMessage(
        text=text.strip(),
        chat_id=chat_id,
        open_id=open_id,
        message_id=message.get("message_id"),
        chat_type=message.get("chat_type"),
        event_id=header.get("event_id") if isinstance(header.get("event_id"), str) else None,
    )


# ---------------------------------------------------------------------------
# Outbound API
# ---------------------------------------------------------------------------
class FeishuApiError(RuntimeError):
    """Feishu refused a request or could not be reached."""


class FeishuReply:
    """Send messages to Feishu with a cached tenant access token.

    The token is fetched lazily on the first reply and cached until shortly
    before its expiry.  One instance is shared by every request thread, so the
    cache is guarded by a lock.
    """

    def __init__(
        self,
        settings: FeishuSettings,
        *,
        opener: Any = urllib.request.urlopen,
        clock: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0

    # -- HTTP -----------------------------------------------------------
    def _post_json(self, url: str, payload: Mapping[str, Any], *, token: str | None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise FeishuApiError(
                sanitize_text(f"feishu http {exc.code}: {detail}")
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FeishuApiError(sanitize_text(f"feishu unreachable: {exc}")) from exc
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise FeishuApiError(f"feishu returned non-JSON: {raw[:200]}") from exc
        if not isinstance(data, dict):
            raise FeishuApiError("feishu returned a non-object response")
        return data

    def _access_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._token_expires_at:
                return self._token
            data = self._post_json(
                f"{self.settings.api_base.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal",
                {
                    "app_id": self.settings.app_id,
                    "app_secret": self.settings.app_secret,
                },
                token=None,
            )
            token = data.get("tenant_access_token")
            if data.get("code") not in (0, None) or not isinstance(token, str) or not token:
                raise FeishuApiError(
                    f"feishu auth failed: code={data.get('code')} msg={data.get('msg')}"
                )
            try:
                expires = int(data.get("expire") or 0)
            except (TypeError, ValueError):
                expires = 0
            self._token = token
            self._token_expires_at = now + max(0, expires - _TOKEN_SKEW_SECONDS)
            return token

    # -- public ---------------------------------------------------------
    def reply_text(self, chat_id: str, text: str) -> dict:
        """Send ``text`` to ``chat_id`` as a plain text message."""

        token = self._access_token()
        data = self._post_json(
            f"{self.settings.api_base.rstrip('/')}/open-apis/im/v1/messages"
            f"?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token=token,
        )
        if data.get("code") not in (0, None):
            # A stale token is the one error worth invalidating the cache for.
            if data.get("code") in (99991661, 99991663, 99991668):
                with self._lock:
                    self._token = None
            raise FeishuApiError(
                f"feishu send failed: code={data.get('code')} msg={data.get('msg')}"
            )
        return data


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------
class EventDeduper:
    """Remember recently handled Feishu event ids.

    Feishu re-delivers an event when the webhook answers slower than its
    timeout (a chat turn can take a while), and every delivery would run the
    task again.  The id is recorded *before* processing, so concurrent
    retries during an in-flight turn are ignored as well.  Bounded FIFO so a
    long-lived server cannot grow without limit.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self.max_entries = max(1, int(max_entries))
        self._seen: dict[str, None] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, event_id: str | None) -> bool:
        if not event_id:
            # Without an id there is nothing to compare; process it.
            return False
        with self._lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            while len(self._seen) > self.max_entries:
                self._seen.pop(next(iter(self._seen)))
            return False


@dataclass(slots=True)
class ChannelReply:
    """What the HTTP layer should send back to Feishu."""

    status: int
    body: dict[str, Any]
    log: list[str] = field(default_factory=list)


def _token_ok(payload: Mapping[str, Any], settings: FeishuSettings, note: Any) -> bool:
    """Check Feishu's plain ``token`` field when one is configured."""

    if not settings.requires_token:
        return True
    supplied = payload.get("token")
    if supplied is None:
        header = payload.get("header")
        if isinstance(header, dict):
            supplied = header.get("token")
    if isinstance(supplied, str) and hmac.compare_digest(
        supplied, str(settings.verification_token)
    ):
        return True
    note("[feishu] refused: verification token mismatch")
    return False


def handle_event(
    payload: Any,
    *,
    settings: FeishuSettings,
    brain: Any,
    reply_client: FeishuReply | None = None,
    log_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    deduper: EventDeduper | None = None,
) -> ChannelReply:
    """Handle one Feishu webhook payload.

    ``brain`` only needs a ``reply(text, user_id=...)`` method.  Network
    failures while replying never turn into an HTTP error: Feishu would retry
    and the task would run twice, so the failure is logged instead.
    """

    lines: list[str] = []

    def note(message: str) -> None:
        lines.append(message)
        if log_path:
            append_log(log_path, message, env)

    if not isinstance(payload, dict):
        note("[feishu] refused: payload is not a JSON object")
        return ChannelReply(400, {"error": "payload must be a JSON object"}, lines)

    # 1. Setup handshake.  Feishu sends ``{"type": "url_verification", ...}``
    #    (or, in the v2 shape, a header with that event type) and expects the
    #    challenge echoed verbatim.
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            note("[feishu] url_verification without a challenge")
            return ChannelReply(400, {"error": "missing challenge"}, lines)
        if not _token_ok(payload, settings, note):
            return ChannelReply(401, {"error": "unauthorized"}, lines)
        note("[feishu] url_verification challenge answered")
        return ChannelReply(200, {"challenge": challenge}, lines)

    header = payload.get("header")
    if isinstance(header, dict) and header.get("event_type") == "url_verification":
        event = payload.get("event")
        challenge = (
            (event.get("challenge") if isinstance(event, dict) else None)
            or payload.get("challenge")
        )
        if not isinstance(challenge, str) or not challenge:
            note("[feishu] url_verification without a challenge")
            return ChannelReply(400, {"error": "missing challenge"}, lines)
        if not _token_ok(payload, settings, note):
            return ChannelReply(401, {"error": "unauthorized"}, lines)
        note("[feishu] url_verification challenge answered")
        return ChannelReply(200, {"challenge": challenge}, lines)

    if not _token_ok(payload, settings, note):
        return ChannelReply(401, {"error": "unauthorized"}, lines)

    try:
        message = parse_event(payload)
    except FeishuEventError as exc:
        note(f"[feishu] ignored malformed event: {exc}")
        return ChannelReply(200, {"ok": True}, lines)

    if message is None:
        note("[feishu] ignored event (not a user text message)")
        return ChannelReply(200, {"ok": True}, lines)

    if deduper is not None and deduper.is_duplicate(message.event_id):
        note(f"[feishu] duplicate delivery ignored: event {message.event_id}")
        return ChannelReply(200, {"ok": True, "duplicate": True}, lines)

    user_id = f"feishu:{message.open_id or message.chat_id}"
    note(
        f"[feishu] message from {user_id} in {message.chat_id}: "
        f"{sanitize_text(message.text, env)[:200]}"
    )

    try:
        answer = brain.reply(message.text, user_id=user_id)
        reply_text = getattr(answer, "text", str(answer))
        mode = getattr(answer, "mode", "?")
    except Exception as exc:  # noqa: BLE001 - the webhook must still answer 200
        note(f"[feishu] brain failed: {type(exc).__name__}: {exc}")
        reply_text = "抱歉，处理这条消息时出错了。"  # "sorry, something went wrong"
        mode = "error"

    note(f"[feishu] reply ({mode}): {sanitize_text(reply_text, env)[:200]}")

    if not settings.has_credentials:
        note("[feishu] no FEISHU_APP_ID/FEISHU_APP_SECRET; reply logged only")
        return ChannelReply(200, {"ok": True, "replied": False}, lines)

    client = reply_client or FeishuReply(settings)
    try:
        client.reply_text(message.chat_id, reply_text)
        note("[feishu] reply sent")
        return ChannelReply(200, {"ok": True, "replied": True}, lines)
    except FeishuApiError as exc:
        note(f"[feishu] reply failed: {exc}")
        # Acknowledged anyway: the task already ran, a Feishu retry would
        # execute it a second time.
        return ChannelReply(200, {"ok": True, "replied": False}, lines)
