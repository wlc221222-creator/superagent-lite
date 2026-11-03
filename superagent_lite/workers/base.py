"""Worker adapter interface.

A worker receives a ticket and its isolated working directory, performs the
task, and returns a :class:`WorkerResult`.  Workers should append their raw
stdout/stderr to ``ticket.log_path`` when it is set; the router writes the
lifecycle entries around it.

Workers that speak the ``RESULT`` contract (see :mod:`superagent_lite.result`)
set ``result_contract = True``; the router then decides the ticket status from
the last ``RESULT`` line instead of trusting the exit code or the prose.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Iterable, Mapping

from ..models import Ticket
from ..logs import append_log

__all__ = ["Worker", "WorkerResult", "append_log", "build_env"]


@dataclass(slots=True)
class WorkerResult:
    """Outcome of one worker execution.

    ``output`` holds the worker's stdout, ``stderr`` its stderr (kept apart so
    that contract parsing always sees the model's final line).
    """

    success: bool
    summary: str = ""
    output: str = ""
    stderr: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration: float = 0.0
    artifacts: list[str] = field(default_factory=list)


class Worker(ABC):
    """Base class for execution-layer adapters."""

    name: ClassVar[str] = "worker"
    #: Whether the router must judge this worker by its ``RESULT`` line.
    result_contract: ClassVar[bool] = False

    @abstractmethod
    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        """Execute ``ticket`` inside ``workdir`` and return the result."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


def build_env(
    passthrough: Iterable[str],
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from an allow-list.

    Only variables named in ``passthrough`` are copied from ``base`` (default:
    this process' environment).  Everything else -- credentials included --
    is dropped.  ``PATH`` is guaranteed so the child can find its helpers.
    """

    source = os.environ if base is None else base
    env = {
        name: source[name]
        for name in passthrough
        if name in source and source[name] != ""
    }
    env.setdefault("PATH", source.get("PATH", os.defpath))
    return env
