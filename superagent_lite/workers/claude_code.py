"""Adapter for the local Claude Code CLI.

Runs ``claude -p "<task + result contract>"`` as a subprocess in the ticket's
isolated working directory, captures stdout/stderr into the ticket log, and
enforces a timeout by killing the whole process group.

Safety boundary
---------------
* Only the tools listed in ``claude_allowed_tools`` are granted.
* ``--add-dir <workdir>`` is the only directory the agent is pointed at.
* ``--max-turns`` bounds runaway loops.
* The child environment is built from an allow-list, never inherited wholesale.
* Everything written to the log is redacted first.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..config import DEFAULT_ALLOWED_TOOLS, DEFAULT_ENV_PASSTHROUGH, resolve_claude_binary
from ..logs import append_log
from ..models import Ticket
from ..result import build_prompt
from .base import Worker, WorkerResult, build_env

HEARTBEAT_INTERVAL = 15.0


class ClaudeCodeWorker(Worker):
    name = "claude"
    result_contract = True

    def __init__(
        self,
        model: str | None = None,
        binary: str = "claude",
        extra_args: Sequence[str] | None = None,
        *,
        allowed_tools: Sequence[str] | None = None,
        max_turns: int = 25,
        permission_mode: str | None = "acceptEdits",
        env_passthrough: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ) -> None:
        self.model = model or None
        self.binary = binary or "claude"
        self.extra_args = list(extra_args or [])
        self.allowed_tools = list(allowed_tools or DEFAULT_ALLOWED_TOOLS)
        self.max_turns = int(max_turns)
        self.permission_mode = permission_mode or None
        self.env_passthrough = list(
            DEFAULT_ENV_PASSTHROUGH if env_passthrough is None else env_passthrough
        )
        # Source environment, injectable for tests.  ``None`` means os.environ.
        self.env = env
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))

    # -- command construction ------------------------------------------
    def resolved_binary(self) -> str:
        return resolve_claude_binary(
            {"claude_bin": None, "claude_binary": self.binary}, env=self.env
        )

    def build_command(self, task: str, workdir: str | os.PathLike[str] | None = None) -> list[str]:
        cwd = str(workdir) if workdir is not None else "."
        command = [
            self.resolved_binary(),
            "-p",
            build_prompt(task, cwd),
            "--allowedTools",
            ",".join(self.allowed_tools),
            "--add-dir",
            cwd,
            "--max-turns",
            str(self.max_turns),
        ]
        if self.permission_mode:
            command.extend(["--permission-mode", self.permission_mode])
        if self.model:
            command.extend(["--model", self.model])
        command.extend(self.extra_args)
        return command

    def child_env(self) -> dict[str, str]:
        return build_env(self.env_passthrough, base=self.env)

    # -- execution ------------------------------------------------------
    def run(
        self,
        ticket: Ticket,
        workdir: Path,
        on_heartbeat: Callable[[], None] | None = None,
    ) -> WorkerResult:
        started = time.monotonic()
        command = self.build_command(ticket.description, workdir)
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    f"$ {shlex.join(command)}",
                    f"[claude] cwd={workdir} timeout={ticket.timeout:g}s "
                    f"max_turns={self.max_turns}",
                ]
            )
            + "\n",
        )

        # The router normally drives the heartbeat (it owns the ticket row);
        # the adapter can also tick on its own when used standalone.
        ticker = _Heartbeat(on_heartbeat, self.heartbeat_interval)

        try:
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                env=self.child_env(),
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
                f"claude binary not found: {self.resolved_binary()!r}. Install "
                "Claude Code, put it on PATH, or set claude_bin in config.json."
            )
            append_log(ticket.log_path, f"[claude] error: {message}\n")
            return WorkerResult(success=False, error=message, duration=duration)
        except OSError as exc:
            duration = time.monotonic() - started
            message = f"failed to start claude: {exc}"
            append_log(ticket.log_path, f"[claude] error: {message}\n")
            return WorkerResult(success=False, error=message, duration=duration)

        timed_out = False
        ticker.start()
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
        finally:
            ticker.stop()

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
                output=stdout,
                stderr=stderr,
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
                output=stdout,
                stderr=stderr,
                error=error,
                exit_code=process.returncode,
                duration=duration,
            )

        # Exit code 0 proves only that the process ended.  The router judges
        # the ``RESULT`` line; this flag marks the run as ready for that
        # judgement rather than as a success.
        return WorkerResult(
            success=True,
            summary=stdout.strip(),
            output=stdout,
            stderr=stderr,
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


class _Heartbeat:
    """Background ticker used when the adapter runs outside a router."""

    def __init__(self, callback: Callable[[], None] | None, interval: float) -> None:
        self.callback = callback
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.callback is None:
            return
        self._thread = threading.Thread(target=self._loop, name="claude-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                assert self.callback is not None
                self.callback()
            except Exception:  # pragma: no cover - a tick must never kill a run
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
