"""Ticket log writing with redaction applied to every line."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .redact import sanitize_text


def append_log(
    log_path: str | Path | None,
    text: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """Append ``text`` to a ticket log, creating parent directories.

    The text is redacted before it touches disk (see :mod:`.redact`).
    """

    if not log_path:
        return
    text = sanitize_text(text, env)
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if text and not text.endswith("\n"):
            handle.write("\n")
