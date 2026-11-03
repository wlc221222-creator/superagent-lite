"""Minimal client for an Anthropic-compatible ``/v1/messages`` endpoint.

The Brain talks to a planning model over the same wire protocol Claude Code
uses, but without any SDK: :mod:`urllib.request` and :mod:`json` are enough.

Credentials are **never** part of this repository or its config.  The base URL
and token are resolved at request time from the ``env`` block of
``~/.claude/settings.json`` (the file Claude Code itself uses), falling back to
the live process environment.  Only the model name is configurable here.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .redact import sanitize_text

DEFAULT_SETTINGS_PATH = "~/.claude/settings.json"
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "superagent-lite/0.2"

# How many times a transient transport failure is retried, and how long to
# wait before each retry.  A 4xx is never retried: it will not get better.
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.5

# An error body is truncated before it is embedded in an exception: enough to
# diagnose, never enough to page through.
_MAX_ERROR_BODY = 500


class PlannerError(RuntimeError):
    """The planner endpoint failed or answered in an unusable way."""


class PlannerConfigError(PlannerError):
    """No usable endpoint/token could be resolved."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where and how to call the model. ``token`` must never be logged."""

    base_url: str
    token: str

    @property
    def messages_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Endpoint(base_url={self.base_url!r}, token='***')"


def _settings_env(settings_path: str | Path) -> Mapping[str, Any]:
    """Read the ``env`` block of a Claude Code settings file."""

    path = Path(settings_path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PlannerConfigError(f"cannot read planner settings {path}: {exc}") from exc
    env = raw.get("env") if isinstance(raw, dict) else None
    return env if isinstance(env, dict) else {}


def load_endpoint(
    settings_path: str | Path = DEFAULT_SETTINGS_PATH,
    environ: Mapping[str, str] | None = None,
) -> Endpoint:
    """Resolve the planner endpoint at runtime.

    Order for each value: ``~/.claude/settings.json``'s ``env`` block first
    (the explicit instruction for this project), then the live environment.
    ``ANTHROPIC_AUTH_TOKEN`` is preferred over ``ANTHROPIC_API_KEY``.

    The returned :class:`Endpoint` is the only place the token lives in
    process memory; it is not written to config, tickets or logs.
    """

    source = os.environ if environ is None else environ
    settings = _settings_env(settings_path)

    def value(name: str) -> str:
        from_settings = settings.get(name)
        if isinstance(from_settings, str) and from_settings.strip():
            return from_settings.strip()
        from_env = source.get(name)
        return from_env.strip() if isinstance(from_env, str) and from_env.strip() else ""

    base_url = value("ANTHROPIC_BASE_URL")
    token = value("ANTHROPIC_AUTH_TOKEN") or value("ANTHROPIC_API_KEY")

    if not base_url:
        raise PlannerConfigError(
            "planner endpoint not configured: set ANTHROPIC_BASE_URL in the "
            f"env block of {Path(settings_path).expanduser()} (or the environment)"
        )
    if not token:
        raise PlannerConfigError(
            "planner token not configured: set ANTHROPIC_AUTH_TOKEN in the "
            f"env block of {Path(settings_path).expanduser()} (or the environment)"
        )
    return Endpoint(base_url=base_url, token=token)


class MessagesClient:
    """A tiny, synchronous Anthropic-compatible chat client."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        timeout: float = 120.0,
        opener: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = max(0.1, float(timeout))
        # ``urllib.request.urlopen`` by default; tests may substitute a stub.
        self._opener = opener
        self._sleep = sleep

    # -- public API -----------------------------------------------------
    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> str:
        """Send one conversation and return the assistant's text."""

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens),
            "messages": [dict(message) for message in messages],
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = float(temperature)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            # Both auth spellings are sent on purpose: ``ANTHROPIC_AUTH_TOKEN``
            # gateways read Bearer, plain API deployments read x-api-key.
            "authorization": f"Bearer {self.endpoint.token}",
            "x-api-key": self.endpoint.token,
            "user-agent": USER_AGENT,
        }

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                self.endpoint.messages_url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                raw = self._send(request)
            except _RetryablePlannerError as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    self._sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise PlannerError(str(exc)) from None
            except PlannerError:
                raise
            return _extract_text(raw)

        raise PlannerError(str(last_error or "planner request failed"))  # pragma: no cover

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            if self._opener is not None:
                response = self._opener(request, timeout=self.timeout)
                with response:
                    status = getattr(response, "status", 200)
                    body = response.read()
            else:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    body = response.read()
        except urllib.error.HTTPError as exc:
            detail = _read_limited(exc)
            message = f"planner HTTP {exc.code}"
            if detail:
                message += f": {detail}"
            if exc.code >= 500 or exc.code == 429:
                raise _RetryablePlannerError(self._redact(message)) from None
            raise PlannerError(self._redact(message)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _RetryablePlannerError(
                self._redact(f"planner request failed: {reason}")
            ) from None

        if not 200 <= int(status) < 300:
            raise PlannerError(f"planner HTTP {status}")
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise PlannerError(f"planner returned invalid JSON: {exc}") from None

    def _redact(self, text: str) -> str:
        """Never let the token leak through an error message."""

        token = self.endpoint.token
        if token:
            text = text.replace(token, "***")
        return sanitize_text(text)


class _RetryablePlannerError(PlannerError):
    """Transient transport/5xx failure; the caller may retry."""


def _read_limited(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read(_MAX_ERROR_BODY * 4)
    except Exception:  # pragma: no cover - error body is best effort
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:_MAX_ERROR_BODY]


def _extract_text(raw: Any) -> str:
    """Pull the assistant text out of a Messages API response."""

    if not isinstance(raw, dict):
        raise PlannerError("planner response must be a JSON object")
    content = raw.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            # ``thinking`` blocks are not part of the answer; only text is.
            if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "".join(parts)
    else:
        raise PlannerError("planner response has no content")
    text = text.strip()
    if not text:
        raise PlannerError("planner returned an empty reply")
    return text
