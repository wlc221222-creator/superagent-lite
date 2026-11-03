"""Configuration loading and defaults.

Configuration is plain JSON (standard library only).  If ``config.json`` is
absent the defaults below are used, which keeps the MVP runnable out of the
box and makes the echo worker the safe default.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

CONFIG_FILENAME = "config.json"

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

#: Descriptions matching any of these (case-insensitive, anywhere) park the
#: ticket in ``awaiting_approval``.  Code, not a model: the list is auditable
#: and deterministic.  Override with ``config.gate_patterns``.
DEFAULT_GATE_PATTERNS: tuple[str, ...] = (
    r"删除|delete|remove|rm\s",
    r"发布|publish|deploy",
    r"发送|send.*(?:mail|email|message)",
    r"付款|支付|\bpay\b",
    r"git push",
)

# Environment variables that are always passed to the Claude Code child.
# Everything else is dropped unless listed in ``claude_env_passthrough``.
DEFAULT_ENV_PASSTHROUGH = [
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "USER",
    "SHELL",
    # Network / TLS plumbing: the CLI is useless without it in proxied or
    # private-CA environments, and it carries no credentials here.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "default_worker": "echo",
    "claude_binary": "claude",
    "claude_bin": None,
    "claude_model": "cn:deepseek-v4.1-flash",
    "claude_extra_args": [],
    "claude_allowed_tools": list(DEFAULT_ALLOWED_TOOLS),
    "claude_max_turns": 25,
    # ``acceptEdits`` lets the allow-listed file tools run without an
    # interactive prompt; every tool outside ``claude_allowed_tools`` is still
    # denied.  Set to null to let Claude Code use its own default.
    "claude_permission_mode": "acceptEdits",
    "claude_env_passthrough": list(DEFAULT_ENV_PASSTHROUGH),
    # The native worker is our own agent loop over the Anthropic-compatible
    # Messages API -- no external coding-agent CLI.  It reads its endpoint
    # from the same settings file as the Brain unless overridden here.
    "native_model": "cn:deepseek-v4.1-flash",
    "native_max_turns": 25,
    "native_settings_path": "~/.claude/settings.json",
    "native_bash_timeout_seconds": 120.0,
    "timeout_seconds": 300.0,
    "stale_seconds": 300.0,
    "heartbeat_seconds": 15.0,
    # How many times an interrupted ticket may be put back on the queue
    # before it is failed for good.
    "max_retries": 1,
    # superagentd: worker pool size, queue poll interval, watchdog interval
    # and how long a graceful shutdown waits for in-flight runs.
    "daemon_concurrency": 2,
    "daemon_poll_seconds": 0.5,
    "daemon_watchdog_seconds": 5.0,
    "daemon_shutdown_grace_seconds": 30.0,
    "runtime_dir": "runtime",
    # Brain (planner).  It speaks the Anthropic-compatible /v1/messages API;
    # the base URL and token are read at request time from
    # ``~/.claude/settings.json``'s ``env`` block (or the live environment).
    # Only the model name lives here -- never a credential.
    "brain_model": "cn:deepseek-v4.1-flash",
    # Optional stronger planner model, used only when the planner flags the
    # request as complex *and* this is set.  ``null`` keeps planning on the
    # cheap default.
    "brain_advanced_model": None,
    # Only escalate to ``brain_advanced_model`` when the planner marks the
    # request ``"complex": true``.  Off by default: expensive models are for
    # planning only, and only when explicitly enabled.
    "brain_escalate_on_complex": False,
    "brain_max_tickets": 8,
    "brain_settings_path": "~/.claude/settings.json",
    "brain_max_tokens": 2048,
    "brain_timeout_seconds": 120.0,
    # Worker used for tickets whose worker the planner did not name.
    "brain_worker": "claude",
    "memory_path": "memory.md",
    "brain_memory_max_chars": 2000,
    # Per-user conversation memory: how many earlier exchanges the planner
    # sees, and the character budget for them.  0 disables history entirely.
    "brain_history_turns": 6,
    "brain_history_max_chars": 6000,
    # ``chat`` / ``POST /chat`` wait this long for tickets before replying
    # with ticket ids instead of a summary.
    "chat_timeout_seconds": 120.0,
    "chat_poll_seconds": 0.25,
    "http_host": "127.0.0.1",
    "http_port": 8765,
    # --- Phase 5: semantic review, approval gate, decision log ----------
    # ``review_mode`` is off | shadow | enforce.  Off in code defaults; the
    # deployment config starts in shadow.  Enforce is a config change (with a
    # kill switch back to shadow/off) -- never a redeploy.
    "review_mode": "off",
    # ``review_enabled`` is the legacy on/off switch; it is kept for the
    # ``review_enabled: true`` + ``review_model`` spelling, but ``review_mode``
    # wins when it is not ``off``.
    "review_enabled": False,
    # null => review is inactive unless a model is configured.  See
    # ``normalize_config``: review_mode != off with a null model disables it
    # loudly instead of failing open.
    "review_model": None,
    # Shadow-phase second runner (Revision 3 R1); informational for the eval
    # harness and the promotion decision.  Never called from the ticket path.
    "review_shadow_model": None,
    # Post-promotion spot checker; recorded for the promotion review only.
    "review_spotcheck_model": None,
    "review_max_artifact_bytes": 20000,
    "review_max_total_bytes": 60000,
    "review_max_retries": 1,
    "review_max_tokens": 1024,
    # MUST stay below ``stale_seconds``; normalize_config clamps it and logs.
    "review_timeout_seconds": 60.0,
    "review_max_feedback_chars": 1000,
    # Minimal approval gate: deterministic regex gate, no model involved.
    "gate_enabled": True,
    "gate_patterns": list(DEFAULT_GATE_PATTERNS),
    # Append-only decision log: ``<runtime_dir>/decisions.jsonl``.  A plain
    # sha256 prev-hash chain; write failures are counted and printed, never
    # allowed to block ticket processing.
    "decision_log_enabled": True,
}

_LIST_DEFAULTS = (
    "claude_extra_args",
    "claude_allowed_tools",
    "claude_env_passthrough",
    "gate_patterns",
)


def default_config() -> dict[str, Any]:
    """Return a fresh copy of the default configuration."""

    config = dict(DEFAULT_CONFIG)
    for key in _LIST_DEFAULTS:
        config[key] = list(DEFAULT_CONFIG[key])
    return config


def normalize_config(values: dict[str, Any] | None) -> dict[str, Any]:
    """Merge user values over the defaults and validate the result."""

    if values is None:
        return default_config()
    if not isinstance(values, dict):
        raise ValueError("config must be a JSON object (dict)")

    config = default_config()
    config.update(values)

    if not isinstance(config["default_worker"], str) or not config["default_worker"]:
        raise ValueError("config.default_worker must be a non-empty string")
    if not isinstance(config["claude_binary"], str) or not config["claude_binary"]:
        raise ValueError("config.claude_binary must be a non-empty string")
    if config["claude_bin"] is not None and (
        not isinstance(config["claude_bin"], str) or not config["claude_bin"]
    ):
        raise ValueError("config.claude_bin must be a non-empty string or null")
    if config["claude_model"] is not None and not isinstance(config["claude_model"], str):
        raise ValueError("config.claude_model must be a string or null")
    if config["claude_permission_mode"] is not None and (
        not isinstance(config["claude_permission_mode"], str)
        or not config["claude_permission_mode"]
    ):
        raise ValueError("config.claude_permission_mode must be a non-empty string or null")

    for key in ("claude_extra_args", "claude_allowed_tools", "claude_env_passthrough"):
        if not isinstance(config[key], list) or not all(
            isinstance(item, str) for item in config[key]
        ):
            raise ValueError(f"config.{key} must be a list of strings")
    config["claude_allowed_tools"] = [
        tool.strip() for tool in config["claude_allowed_tools"] if tool.strip()
    ]
    if not config["claude_allowed_tools"]:
        raise ValueError("config.claude_allowed_tools must not be empty")
    config["claude_env_passthrough"] = [
        name.strip() for name in config["claude_env_passthrough"] if name.strip()
    ]

    if isinstance(config["claude_max_turns"], bool) or not isinstance(
        config["claude_max_turns"], int
    ):
        raise ValueError("config.claude_max_turns must be an integer")
    if config["claude_max_turns"] <= 0:
        raise ValueError("config.claude_max_turns must be greater than 0")

    for key in (
        "timeout_seconds",
        "stale_seconds",
        "heartbeat_seconds",
        "daemon_poll_seconds",
        "daemon_watchdog_seconds",
    ):
        try:
            number = float(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.{key} must be a number") from exc
        if number <= 0:
            raise ValueError(f"config.{key} must be greater than 0")
        config[key] = number

    grace = config["daemon_shutdown_grace_seconds"]
    try:
        grace = float(grace)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "config.daemon_shutdown_grace_seconds must be a number"
        ) from exc
    if grace < 0:
        raise ValueError("config.daemon_shutdown_grace_seconds must be >= 0")
    config["daemon_shutdown_grace_seconds"] = grace

    for key, minimum in (("daemon_concurrency", 1), ("max_retries", 0)):
        if isinstance(config[key], bool) or not isinstance(config[key], int):
            raise ValueError(f"config.{key} must be an integer")
        if config[key] < minimum:
            raise ValueError(f"config.{key} must be >= {minimum}")

    if not isinstance(config["runtime_dir"], (str, Path)) or not str(config["runtime_dir"]):
        raise ValueError("config.runtime_dir must be a non-empty path string")

    for key in ("brain_model", "brain_worker", "brain_settings_path", "memory_path"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"config.{key} must be a non-empty string")
    if config["brain_advanced_model"] is not None and (
        not isinstance(config["brain_advanced_model"], str)
        or not config["brain_advanced_model"].strip()
    ):
        raise ValueError("config.brain_advanced_model must be a non-empty string or null")
    if isinstance(config["brain_max_tokens"], bool) or not isinstance(
        config["brain_max_tokens"], int
    ):
        raise ValueError("config.brain_max_tokens must be an integer")
    if config["brain_max_tokens"] <= 0:
        raise ValueError("config.brain_max_tokens must be greater than 0")

    # --- native worker -------------------------------------------------
    # ``native_settings_path`` deliberately follows ``brain_settings_path``
    # unless set explicitly: both speak the same Messages endpoint and read it
    # from the same Claude Code settings file.
    if values.get("native_settings_path") in (None, ""):
        config["native_settings_path"] = config["brain_settings_path"]
    for key in ("native_model", "native_settings_path"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"config.{key} must be a non-empty string")
        config[key] = config[key].strip()
    if isinstance(config["native_max_turns"], bool) or not isinstance(
        config["native_max_turns"], int
    ):
        raise ValueError("config.native_max_turns must be an integer")
    if config["native_max_turns"] <= 0:
        raise ValueError("config.native_max_turns must be greater than 0")
    try:
        bash_timeout = float(config["native_bash_timeout_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "config.native_bash_timeout_seconds must be a number"
        ) from exc
    if bash_timeout <= 0:
        raise ValueError("config.native_bash_timeout_seconds must be greater than 0")
    config["native_bash_timeout_seconds"] = bash_timeout
    if isinstance(config["brain_max_tickets"], bool) or not isinstance(
        config["brain_max_tickets"], int
    ):
        raise ValueError("config.brain_max_tickets must be an integer")
    if config["brain_max_tickets"] < 1:
        raise ValueError("config.brain_max_tickets must be at least 1")
    if not isinstance(config["brain_escalate_on_complex"], bool):
        raise ValueError("config.brain_escalate_on_complex must be a boolean")

    for key in (
        "brain_timeout_seconds",
        "chat_timeout_seconds",
        "chat_poll_seconds",
    ):
        try:
            number = float(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.{key} must be a number") from exc
        if number <= 0:
            raise ValueError(f"config.{key} must be greater than 0")
        config[key] = number

    if isinstance(config["brain_memory_max_chars"], bool) or not isinstance(
        config["brain_memory_max_chars"], int
    ):
        raise ValueError("config.brain_memory_max_chars must be an integer")
    if config["brain_memory_max_chars"] < 0:
        raise ValueError("config.brain_memory_max_chars must be >= 0")

    for key in ("brain_history_turns", "brain_history_max_chars"):
        if isinstance(config[key], bool) or not isinstance(config[key], int):
            raise ValueError(f"config.{key} must be an integer")
        if config[key] < 0:
            raise ValueError(f"config.{key} must be >= 0")

    if isinstance(config["http_port"], bool) or not isinstance(config["http_port"], int):
        raise ValueError("config.http_port must be an integer")
    if not 0 <= config["http_port"] <= 65535:
        raise ValueError("config.http_port must be between 0 and 65535")
    if not isinstance(config["http_host"], str) or not config["http_host"].strip():
        raise ValueError("config.http_host must be a non-empty string")

    config = _normalize_phase5(config)
    return config


# ---------------------------------------------------------------------------
# Phase 5 keys
# ---------------------------------------------------------------------------
REVIEW_MODES = ("off", "shadow", "enforce")


def _normalize_phase5(config: dict[str, Any]) -> dict[str, Any]:
    """Validate review/gate/decision-log keys and apply the startup rules.

    Validation errors raise (a config typo must not be silently ignored);
    the *policy* decisions -- disabling review when its model is missing,
    clamping ``review_timeout_seconds`` below ``stale_seconds`` -- are recorded
    on ``config["_warnings"]`` so the CLI/daemon can print them loudly instead
    of aborting a self-hosted install over a soft setting.
    """

    warnings: list[str] = []
    mode = config.get("review_mode", "off")
    if not isinstance(mode, str) or mode.strip().lower() not in REVIEW_MODES:
        raise ValueError(
            "config.review_mode must be one of: " + ", ".join(REVIEW_MODES)
        )
    mode = mode.strip().lower()
    if not isinstance(config.get("review_enabled"), bool):
        raise ValueError("config.review_enabled must be a boolean")
    if config["review_enabled"] and mode == "off":
        # The phase-5 spec's legacy spelling: ``review_enabled: true`` means
        # shadow mode.  It never means "enforce" -- promoting is explicit.
        mode = "shadow"
    config["review_mode"] = mode

    for key in ("review_model", "review_shadow_model", "review_spotcheck_model"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"config.{key} must be a non-empty string or null")
        config[key] = value.strip() if isinstance(value, str) else None

    if mode != "off" and not config["review_model"]:
        warnings.append(
            f"review_mode={mode!r} but review_model is null; review is DISABLED "
            "for this process (set review_model to a model name)"
        )
        config["review_mode"] = "off"
    elif mode != "off" and config["review_model"] == config.get("brain_model"):
        warnings.append(
            "review_model equals brain_model; the review is not an independent "
            "cross-check of the planner (same vendor/model family)"
        )

    for key, minimum in (
        ("review_max_artifact_bytes", 1),
        ("review_max_total_bytes", 1),
        ("review_max_tokens", 1),
        ("review_max_retries", 0),
        ("review_max_feedback_chars", 1),
    ):
        if isinstance(config[key], bool) or not isinstance(config[key], int):
            raise ValueError(f"config.{key} must be an integer")
        if config[key] < minimum:
            raise ValueError(f"config.{key} must be >= {minimum}")
    if config["review_max_total_bytes"] < config["review_max_artifact_bytes"]:
        raise ValueError(
            "config.review_max_total_bytes must be >= review_max_artifact_bytes"
        )

    try:
        timeout = float(config["review_timeout_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError("config.review_timeout_seconds must be a number") from exc
    if timeout <= 0:
        raise ValueError("config.review_timeout_seconds must be greater than 0")
    stale = float(config["stale_seconds"])
    if timeout >= stale:
        clamped = max(0.001, stale / 2.0)
        warnings.append(
            f"review_timeout_seconds={timeout:g} >= stale_seconds={stale:g}; "
            f"clamped to {clamped:g}s so a review can never outlive its lease"
        )
        timeout = clamped
    config["review_timeout_seconds"] = timeout

    if not isinstance(config.get("gate_enabled"), bool):
        raise ValueError("config.gate_enabled must be a boolean")
    patterns = config.get("gate_patterns")
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) for pattern in patterns
    ):
        raise ValueError("config.gate_patterns must be a list of regex strings")
    for pattern in patterns:
        if not pattern.strip():
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"config.gate_patterns contains an invalid regex {pattern!r}: {exc}"
            ) from exc
    config["gate_patterns"] = [pattern for pattern in patterns if pattern.strip()]

    if not isinstance(config.get("decision_log_enabled"), bool):
        raise ValueError("config.decision_log_enabled must be a boolean")

    config["_warnings"] = warnings
    return config


def resolve_claude_binary(
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> str:
    """Resolve the Claude Code executable.

    Order: explicit ``claude_bin`` from config -> ``PATH`` -> well-known
    locations (``~/.local/bin/claude``, then ``claude_binary`` as-is).

    The returned value is the command to execute; callers still get a clear
    "not found" failure from the worker when the path does not exist.
    """

    explicit = config.get("claude_bin")
    if explicit:
        return str(Path(explicit).expanduser())

    binary = str(config.get("claude_binary") or "claude")
    if Path(binary).is_absolute() or "/" in binary or "\\" in binary:
        return str(Path(binary).expanduser())

    source = os.environ if env is None else env
    search_path = source.get("PATH")
    found = shutil.which(binary, path=search_path or os.defpath)
    if found:
        return found

    base_home = Path(home).expanduser() if home is not None else Path.home()
    fallback = base_home / ".local" / "bin" / binary
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return binary


def load_config(path: str | Path | None = None, *, require: bool = False) -> dict[str, Any]:
    """Load ``config.json``.

    With no explicit path, an existing ``./config.json`` is used and a missing
    one silently falls back to defaults.  An explicitly supplied path that
    does not exist raises ``FileNotFoundError`` when ``require`` is true.
    """

    if path is None:
        config_path = Path(CONFIG_FILENAME)
        required = False
    else:
        config_path = Path(path).expanduser()
        required = require

    if not config_path.exists():
        if required:
            raise FileNotFoundError(f"config file not found: {config_path}")
        return default_config()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {config_path}: {exc}") from exc

    return normalize_config(raw)
