"""Worker adapter interface.

A worker receives a ticket and its isolated working directory, performs the
task, and returns a :class:`WorkerResult`.  Workers should append their raw
stdout/stderr to ``ticket.log_path`` when it is set; the router writes the
lifecycle entries around it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..models import Ticket


@dataclass(slots=True)
class WorkerResult:
    """Outcome of one worker execution."""

    success: bool
    summary: str = ""
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration: float = 0.0


class Worker(ABC):
    """Base class for execution-layer adapters."""

    name: ClassVar[str] = "worker"

    @abstractmethod
    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        """Execute ``ticket`` inside ``workdir`` and return the result."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


def append_log(log_path: str | Path | None, text: str) -> None:
    """Append ``text`` to a ticket log, creating parent directories."""

    if not log_path:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if text and not text.endswith("\n"):
            handle.write("\n")
