"""Minimal long-term memory: a plain ``memory.md`` injected into the planner.

The file is maintained by hand (no automatic distillation yet, as phase 3
specifies).  It is truncated to ``brain_memory_max_chars`` so a bloated file
can never crowd the planner's context: the beginning is kept, since that is
where a human writes the durable preferences first.
"""

from __future__ import annotations

from pathlib import Path

TRUNCATION_MARKER = "\n...(memory truncated; see memory.md)"


def read_memory(path: str | Path, max_chars: int = 2000) -> str:
    """Return the memory text, clipped to ``max_chars`` characters.

    A missing or unreadable file is simply empty memory -- memory is an
    enhancement, never a reason for a chat to fail.
    """

    if max_chars <= 0:
        return ""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(TRUNCATION_MARKER))
    return text[:keep].rstrip() + TRUNCATION_MARKER
