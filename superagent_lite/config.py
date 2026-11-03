"""Configuration loading and defaults.

Configuration is plain JSON (standard library only).  If ``config.json`` is
absent the defaults below are used, which keeps the MVP runnable out of the
box and makes the echo worker the safe default.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

CONFIG_FILENAME = "config.json"

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

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
    "claude_model": "claude-sonnet-5-5",
    "claude_extra_args": [],
    "claude_allowed_tools": list(DEFAULT_ALLOWED_TOOLS),
    "claude_max_turns": 25,
    # ``acceptEdits`` lets the allow-listed file tools run without an
    # interactive prompt; every tool outside ``claude_allowed_tools`` is still
    # denied.  Set to null to let Claude Code use its own default.
    "claude_permission_mode": "acceptEdits",
    "claude_env_passthrough": list(DEFAULT_ENV_PASSTHROUGH),
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
}

_LIST_DEFAULTS = (
    "claude_extra_args",
    "claude_allowed_tools",
    "claude_env_passthrough",
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
