"""Per-user conversation threads: ``<runtime_dir>/threads/<user>.jsonl``.

One append-only JSONL file per user, one record per completed exchange::

    {"ts": "...", "user": "...", "assistant": "..."}

The store is deliberately small and forgiving:

* ``user_id`` is sanitized to ``[A-Za-z0-9_.-]``; anything else (a Feishu
  ``open_id`` with ``:`` in it, an odd HTTP client) is mapped to a stable
  filename derived from a sha1 prefix, so a user id can never escape the
  directory or collide with another user's file;
* appends take an inter-process ``fcntl.flock`` when the platform has it
  (same guard as :mod:`superagent_lite.decision_log`);
* reads skip corrupt or incomplete lines and tolerate a missing file, because
  history is a convenience -- it must never break a chat reply.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # POSIX only; threads live next to the daemon.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

THREADS_DIRNAME = "threads"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_FALLBACK_PREFIX = "u_"
_FALLBACK_HASH_CHARS = 16


def thread_filename(user_id: str) -> str:
    """Return the JSONL filename for ``user_id`` (sanitized or hashed)."""

    candidate = (user_id or "").strip()
    if (
        candidate
        and candidate not in (".", "..")
        and _SAFE_ID.fullmatch(candidate)
    ):
        return f"{candidate}.jsonl"
    digest = hashlib.sha1((user_id or "").encode("utf-8")).hexdigest()
    return f"{_FALLBACK_PREFIX}{digest[:_FALLBACK_HASH_CHARS]}.jsonl"


class ThreadStore:
    """Append-only, flock-guarded, per-user conversation history."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def path_for(self, user_id: str) -> Path:
        """Path of the file backing ``user_id`` (not created until append)."""

        return self.root / thread_filename(user_id)

    # -- writes ---------------------------------------------------------
    def append(self, user_id: str, user_text: str, assistant_text: str) -> None:
        """Append one exchange; never raises (history is best effort)."""

        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "user": user_text or "",
            "assistant": assistant_text or "",
        }
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            path = self.path_for(user_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.write(line)
                    handle.flush()
                finally:
                    if fcntl is not None:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        except OSError:  # pragma: no cover - close releases it
                            pass
        except Exception:  # noqa: BLE001 - a chat reply must never fail here
            return

    # -- reads ----------------------------------------------------------
    def history(
        self,
        user_id: str,
        max_turns: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        """Return the most recent turns, oldest first, as chat messages.

        ``max_turns`` and ``max_chars`` both bound the result; whole turns are
        dropped oldest-first to fit ``max_chars``.  Each turn becomes a
        ``{"role": "user"}`` message followed by an ``{"role": "assistant"}``
        one.
        """

        if max_turns <= 0 or max_chars <= 0:
            return []
        turns = self._read_turns(user_id)
        if not turns:
            return []
        turns = turns[-max_turns:]

        # Keep the newest turns that fit; a turn that does not fit must not be
        # skipped in favour of an older one -- history stays contiguous.
        kept: list[tuple[str, str]] = []
        used = 0
        for user_text, assistant_text in reversed(turns):
            cost = len(user_text) + len(assistant_text)
            if used + cost > max_chars:
                break
            kept.append((user_text, assistant_text))
            used += cost
        kept.reverse()

        messages: list[dict[str, str]] = []
        for user_text, assistant_text in kept:
            messages.append({"role": "user", "content": user_text})
            messages.append({"role": "assistant", "content": assistant_text})
        return messages

    def _read_turns(self, user_id: str) -> list[tuple[str, str]]:
        path = self.path_for(user_id)
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        turns: list[tuple[str, str]] = []
        for raw in raw_lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            user_text = record.get("user")
            assistant_text = record.get("assistant")
            if not isinstance(user_text, str) or not isinstance(assistant_text, str):
                continue
            turns.append((user_text, assistant_text))
        return turns


__all__ = ["ThreadStore", "thread_filename", "THREADS_DIRNAME"]
