"""Adapter for the local Claude Code CLI.

Runs ``claude -p "<task>"`` as a subprocess in the ticket's isolated working
directory, captures stdout/stderr into the ticket log, and enforces a timeout
by killing the whole process group.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from ..models import Ticket
from .base import Worker, WorkerResult, append_log


class ClaudeCodeWorker(Worker):
    name = "claude"

    def __init__(
        self,
        model: str | None = None,
        binary: str = "claude",
        extra_args: list[str] | None = None,
    ) -> None:
        self.model = model or None
        self.binary = binary or "claude"
        self.extra_args = list(extra_args or [])

    def build_command(self, task: str) -> list[str]:
        command = [self.binary, "-p", task]
        if self.model:
            command.extend(["--model", self.model])
        command.extend(self.extra_args)
        return command

    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        started = time.monotonic()
        command = self.build_command(ticket.description)
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    f"$ {shlex.join(command)}",
                    f"[claude] cwd={workdir} timeout={ticket.timeout:g}s",
                ]
            )
            + "\n",
        )

        try:
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError:
            duration = time.monotonic() - started
            message = (
                f"claude binary not found: {self.binary!r}. Install Claude Code "
                "and make sure it is on PATH."
            )
            append_log(ticket.log_path, f"[claude] error: {message}\n")
            return WorkerResult(
                success=False,
                error=message,
                duration=duration,
            )
        except OSError as exc:
            duration = time.monotonic() - started
            message = f"failed to start claude: {exc}"
            append_log(ticket.log_path, f"[claude] error: {message}\n")
            return WorkerResult(success=False, error=message, duration=duration)

        timed_out = False
        try:
            stdout, stderr = process.communicate(
                timeout=ticket.timeout if ticket.timeout > 0 else None
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()

        duration = time.monotonic() - started
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    "[stdout]",
                    stdout or "",
                    "[stderr]",
                    stderr or "",
                    (
                        f"[claude] exit_code={process.returncode} "
                        f"elapsed={duration:.2f}s timed_out={timed_out}"
                    ),
                ]
            )
            + "\n",
        )

        if timed_out:
            return WorkerResult(
                success=False,
                summary=stdout.strip(),
                output=stdout + stderr,
                error=f"claude timed out after {ticket.timeout:g}s; process group killed",
                exit_code=process.returncode,
                duration=duration,
            )

        if process.returncode != 0:
            details = (stderr or stdout or "").strip()
            error = f"claude exited with code {process.returncode}"
            if details:
                error += f": {_tail(details, 1000)}"
            return WorkerResult(
                success=False,
                summary=stdout.strip(),
                output=stdout + stderr,
                error=error,
                exit_code=process.returncode,
                duration=duration,
            )

        return WorkerResult(
            success=True,
            summary=stdout.strip() or "(claude produced no output)",
            output=stdout + stderr,
            exit_code=0,
            duration=duration,
        )

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        """Kill the child and any grandchildren it spawned."""

        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            process.kill()
        except OSError:
            pass


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
