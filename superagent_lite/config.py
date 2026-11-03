"""Configuration loading and defaults.

Configuration is plain JSON (standard library only).  If ``config.json`` is
absent the defaults below are used, which keeps the MVP runnable out of the
box and makes the echo worker the safe default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "default_worker": "echo",
    "claude_binary": "claude",
    "claude_model": "claude-sonnet-5-5",
    "claude_extra_args": [],
    "timeout_seconds": 300.0,
    "runtime_dir": "runtime",
}


def default_config() -> dict[str, Any]:
    """Return a fresh copy of the default configuration."""

    config = dict(DEFAULT_CONFIG)
    config["claude_extra_args"] = list(DEFAULT_CONFIG["claude_extra_args"])
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
    if config["claude_model"] is not None and not isinstance(config["claude_model"], str):
        raise ValueError("config.claude_model must be a string or null")
    if not isinstance(config["claude_extra_args"], list) or not all(
        isinstance(arg, str) for arg in config["claude_extra_args"]
    ):
        raise ValueError("config.claude_extra_args must be a list of strings")
    try:
        timeout = float(config["timeout_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError("config.timeout_seconds must be a number") from exc
    if timeout <= 0:
        raise ValueError("config.timeout_seconds must be greater than 0")
    config["timeout_seconds"] = timeout

    if not isinstance(config["runtime_dir"], (str, Path)) or not str(config["runtime_dir"]):
        raise ValueError("config.runtime_dir must be a non-empty path string")

    return config


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
