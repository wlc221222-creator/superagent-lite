"""Offline echo worker used for tests and smoke runs."""

from __future__ import annotations

import time
from pathlib import Path

from ..models import Ticket
from .base import Worker, WorkerResult, append_log


class EchoWorker(Worker):
    """A fake worker that echoes the task without calling any external model.

    It honours the same result contract as the Claude adapter by constructing
    a successful :class:`WorkerResult` directly (``result_contract`` is off:
    there is no model reply to parse, and ``success`` is authoritative).
    """

    name = "echo"
    result_contract = False

    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        started = time.monotonic()
        summary = f"echo: {ticket.description}"
        output = "\n".join(
            [
                f"[echo] ticket: {ticket.id}",
                f"[echo] workdir: {workdir}",
                f"[echo] task: {ticket.description}",
                "[echo] completed without calling an external model",
            ]
        )
        append_log(ticket.log_path, output + "\n")
        return WorkerResult(
            success=True,
            summary=summary,
            output=output,
            exit_code=0,
            duration=time.monotonic() - started,
            artifacts=[],
        )
