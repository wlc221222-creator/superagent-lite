"""Codex worker placeholder.

The interface is intentionally identical to the other workers so a real
adapter can be dropped in later without touching the router.  Until then the
router records a clean ``failed`` ticket instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Ticket
from .base import Worker, WorkerResult


class CodexWorker(Worker):
    name = "codex"

    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        raise NotImplementedError(
            "CodexWorker is a placeholder: the Codex CLI adapter is planned for "
            "a later milestone. Use --worker claude or --worker echo for now."
        )
