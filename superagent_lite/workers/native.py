"""In-house execution agent: our own agent loop over the Messages API.

This is the native counterpart to :mod:`~superagent_lite.workers.claude_code`:
instead of shelling out to an external coding-agent CLI, it drives an
Anthropic-compatible model directly and executes the tool calls itself.  The
model is asked for a single task and the loop ends when it answers without
requesting a tool, at which point its text is judged by the usual ``RESULT``
contract (see :mod:`superagent_lite.result`).

Safety boundary
---------------
* Every path a tool receives is resolved against the ticket's working
  directory; ``..``, absolute paths outside it and symlinks that point outside
  it are rejected with ``path escapes the working directory``.
* ``bash`` runs with ``cwd=workdir``, an allow-listed environment (the same
  passthrough style as the Claude adapter) and a hard per-command timeout.
* The loop is bounded by ``max_turns``; the router's ``timeout_seconds`` is
  the outer bound.
* Tool calls and their (truncated) results are appended to the ticket log,
  where :func:`~superagent_lite.logs.append_log` redacts them first.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import DEFAULT_ENV_PASSTHROUGH
from ..logs import append_log
from ..models import Ticket
from ..planner import DEFAULT_SETTINGS_PATH, MessagesClient, PlannerError, load_endpoint
from ..result import build_prompt
from .base import Worker, WorkerResult, build_env
from .claude_code import _Heartbeat

MAX_READ_CHARS = 64_000
MAX_BASH_CHARS = 32_000
MAX_GLOB_ENTRIES = 200
MAX_GREP_MATCHES = 100
#: Files larger than this are skipped by ``grep`` (a text search, not a disk
#: scanner); the bound keeps one stray archive from stalling a whole run.
MAX_GREP_FILE_BYTES = 2_000_000

JAIL_ERROR = "path escapes the working directory"

SYSTEM_PROMPT = """\
You are an execution agent. You receive exactly ONE task and you must carry it \
out yourself using the tools provided.

Rules:
- Do the work inside the working directory only. Every path you pass to a tool \
is resolved relative to that directory; paths that escape it are rejected.
- Use the tools rather than describing what should be done. Read before you \
edit, and verify your work (for example by reading the file back or running it).
- Decide ordinary implementation details yourself. If completing the task \
requires a material user decision that the context does not answer, stop \
with status "failed" and put one concrete question in the summary so the \
main agent can ask it. Do not invent that decision.
- When the task is complete, stop calling tools and reply with the mandatory \
RESULT line described in the task. The RESULT line is the only thing the \
caller reads; prose before it is ignored.
- If the task cannot be completed, say why and use status "failed".
"""

#: Anthropic tool definitions.  The names are part of the worker's contract.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a text file from the working directory (or a subdirectory). "
            "Output is truncated at 64000 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file with the given content. Parent "
            "directories inside the working directory are created as needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace exactly one occurrence of old_text with new_text in an "
            "existing file. Fails if old_text is absent or ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_text": {"type": "string", "description": "Exact text to find"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Run a shell command in the working directory and return its "
            "combined stdout and stderr. Output is truncated at 32000 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "glob",
        "description": (
            "List paths matching a glob pattern relative to the working "
            "directory (e.g. '**/*.py'). At most 200 entries are returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"}
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Recursively search text files for a regular expression. Returns "
            "up to 100 'path:line: text' matches. Binary files and .git are "
            "skipped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default '.')",
                },
            },
            "required": ["pattern"],
        },
    },
]


class NativeWorker(Worker):
    """Drive a model directly and execute its tool calls in a jailed workdir."""

    name = "native"
    result_contract = True

    def __init__(
        self,
        model: str | None = None,
        settings_path: str = DEFAULT_SETTINGS_PATH,
        *,
        max_turns: int = 25,
        max_tokens: int = 4096,
        bash_timeout_seconds: float = 120.0,
        request_timeout: float = 300.0,
        heartbeat_interval: float = 15.0,
        env_passthrough: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        client: Any = None,
        opener: Any = None,
    ) -> None:
        self.model = model or None
        self.settings_path = settings_path or DEFAULT_SETTINGS_PATH
        # At least one turn: the loop below must always make a model call.
        self.max_turns = max(1, int(max_turns))
        self.max_tokens = int(max_tokens)
        self.bash_timeout_seconds = max(0.1, float(bash_timeout_seconds))
        self.request_timeout = max(0.1, float(request_timeout))
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.env_passthrough = list(
            DEFAULT_ENV_PASSTHROUGH if env_passthrough is None else env_passthrough
        )
        # Source environment, injectable for tests.  ``None`` means os.environ.
        self.env = env
        # Tests inject a scripted client; production builds one lazily so a
        # missing settings file fails the ticket, not the router's startup.
        self.client = client
        self.opener = opener
        self._built_client: Any = None

    # -- client ---------------------------------------------------------
    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        if self._built_client is None:
            # ``environ=self.env`` keeps an injected environment (tests) from
            # falling back to whatever the host process happens to export.
            endpoint = load_endpoint(self.settings_path, environ=self.env)
            self._built_client = MessagesClient(
                endpoint,
                timeout=self.request_timeout,
                opener=self.opener,
            )
        return self._built_client

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
        root = Path(workdir).expanduser().resolve()
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    f"[native] cwd={root} model={self.model} "
                    f"max_turns={self.max_turns} max_tokens={self.max_tokens}",
                    f"[native] task: {ticket.description}",
                ]
            )
            + "\n",
        )

        if not self.model:
            return self._failure(
                ticket, started, "native worker has no model configured (native_model)"
            )
        try:
            client = self._client()
        except PlannerError as exc:
            return self._failure(ticket, started, str(exc))

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": build_prompt(ticket.description, root, context=ticket.context),
            }
        ]

        # The router already ticks its own heartbeat thread while the worker
        # runs, so -- exactly like the Claude adapter -- this worker never
        # starts a second ticker.  ``on_heartbeat`` is ticked once per model
        # turn, which keeps a long tool batch visibly alive for callers that
        # drive the worker standalone.
        ticker = _Heartbeat(None, self.heartbeat_interval)
        ticker.start()
        try:
            for turn in range(1, self.max_turns + 1):
                try:
                    response = client.messages(
                        model=self.model,
                        system=SYSTEM_PROMPT,
                        messages=messages,
                        max_tokens=self.max_tokens,
                        tools=TOOLS,
                    )
                except PlannerError as exc:
                    return self._failure(ticket, started, f"model request failed: {exc}")
                except Exception as exc:  # noqa: BLE001 - an injected client may raise
                    return self._failure(
                        ticket, started, f"model request failed: {type(exc).__name__}: {exc}"
                    )
                finally:
                    if on_heartbeat is not None:
                        try:
                            on_heartbeat()
                        except Exception:  # pragma: no cover - a tick must never kill a run
                            pass

                try:
                    content = _normalize_content(response)
                except PlannerError as exc:
                    return self._failure(ticket, started, str(exc))
                text = _text_of(content)
                tool_uses = [block for block in content if block.get("type") == "tool_use"]

                append_log(
                    ticket.log_path,
                    "\n".join(
                        [
                            f"[native] turn {turn}/{self.max_turns} "
                            f"text_chars={len(text)} tool_calls={len(tool_uses)}",
                            f"[native] assistant: {_cap(text, 2000)}" if text else "",
                        ]
                    )
                    + "\n",
                )

                if not tool_uses:
                    duration = time.monotonic() - started
                    append_log(
                        ticket.log_path,
                        f"[native] finished after {turn} turn(s) "
                        f"elapsed={duration:.2f}s\n",
                    )
                    # ``success`` means "the run ended cleanly and its output is
                    # ready for contract judgement", exactly like the Claude
                    # adapter.  A missing RESULT line fails in the router.
                    return WorkerResult(
                        success=True,
                        summary=text.strip(),
                        output=text,
                        exit_code=0,
                        duration=duration,
                    )

                if turn == self.max_turns:
                    # No turn is left to report the tool results in, so
                    # executing them would mutate the workspace after the run
                    # is already over.  Stop here and fail honestly.
                    return self._failure(
                        ticket,
                        started,
                        f"native worker exhausted max_turns ({self.max_turns}) "
                        "without producing a final answer",
                    )

                messages.append({"role": "assistant", "content": content})
                results: list[dict[str, Any]] = []
                for block in tool_uses:
                    name = str(block.get("name") or "")
                    arguments = block.get("input")
                    if not isinstance(arguments, dict):
                        arguments = {}
                    tool_id = str(block.get("id") or f"toolu_{turn}_{len(results)}")
                    output, is_error = self._execute_tool(name, arguments, root)
                    append_log(
                        ticket.log_path,
                        "\n".join(
                            [
                                f"[native] tool {name} input={_cap(_short_json(arguments), 2000)}",
                                f"[native] tool {name} result "
                                f"({'error' if is_error else 'ok'}): {_cap(output, 4000)}",
                            ]
                        )
                        + "\n",
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": output,
                            "is_error": is_error,
                        }
                    )
                messages.append({"role": "user", "content": results})

            # Every iteration above returns; this only guards the type.
            return self._failure(  # pragma: no cover - unreachable
                ticket, started, "native worker ended without a result"
            )
        finally:
            ticker.stop()

    def _failure(self, ticket: Ticket, started: float, message: str) -> WorkerResult:
        duration = time.monotonic() - started
        append_log(ticket.log_path, f"[native] error: {message}\n")
        return WorkerResult(
            success=False,
            summary=message,
            output="",
            stderr=message,
            error=message,
            duration=duration,
        )

    # -- tools ----------------------------------------------------------
    def _execute_tool(
        self, name: str, arguments: Mapping[str, Any], root: Path
    ) -> tuple[str, bool]:
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "bash": self._bash,
            "glob": self._glob,
            "grep": self._grep,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"unknown tool: {name}", True
        try:
            return handler(arguments, root)
        except _ToolError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the loop
            return f"{type(exc).__name__}: {exc}", True

    def _read_file(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        path = self._jail(str(arguments.get("path") or ""), root)
        if not path.is_file():
            return f"not a file: {arguments.get('path')!r}", True
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = (
                text[:MAX_READ_CHARS]
                + f"\n... (truncated: file is {len(text)} characters; "
                f"showing first {MAX_READ_CHARS})"
            )
        return text, False

    def _write_file(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        path = self._jail(str(arguments.get("path") or ""), root)
        content = arguments.get("content")
        if not isinstance(content, str):
            return "content must be a string", True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} characters to {_relative(path, root)}", False

    def _edit_file(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        path = self._jail(str(arguments.get("path") or ""), root)
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            return "old_text must be a non-empty string", True
        if not isinstance(new_text, str):
            return "new_text must be a string", True
        if not path.is_file():
            return f"not a file: {arguments.get('path')!r}", True
        current = path.read_text(encoding="utf-8", errors="replace")
        count = current.count(old_text)
        if count == 0:
            return "old_text not found in file", True
        if count > 1:
            return (
                f"old_text occurs {count} times; it must occur exactly once "
                "to edit safely",
                True,
            )
        path.write_text(current.replace(old_text, new_text, 1), encoding="utf-8")
        return f"edited {_relative(path, root)}", False

    def _bash(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return "command must be a non-empty string", True
        process = subprocess.Popen(
            command,
            cwd=str(root),
            env=self.child_env(),
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
        timed_out = False
        try:
            output, _ = process.communicate(timeout=self.bash_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                process.kill()
                output, _ = process.communicate()
        output = output or ""
        if len(output) > MAX_BASH_CHARS:
            output = (
                output[:MAX_BASH_CHARS]
                + f"\n... (truncated: output is {len(output)} characters; "
                f"showing first {MAX_BASH_CHARS})"
            )
        if timed_out:
            return (
                output
                + f"\n[command timed out after {self.bash_timeout_seconds:g}s "
                "and was killed]",
                True,
            )
        if process.returncode:
            return output + f"\n[exit code {process.returncode}]", True
        return output, False

    def _glob(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return "pattern must be a non-empty string", True
        pattern = pattern.strip()
        if os.path.isabs(pattern) or pattern.startswith("~"):
            raise _ToolError(JAIL_ERROR)
        try:
            matches: list[str] = []
            for candidate in root.glob(pattern):
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if not _inside(resolved, root):
                    raise _ToolError(JAIL_ERROR)
                matches.append(_relative(resolved, root))
        except ValueError as exc:
            return f"invalid glob pattern: {exc}", True
        except NotImplementedError:
            return "invalid glob pattern", True
        matches = sorted(dict.fromkeys(matches))
        if len(matches) > MAX_GLOB_ENTRIES:
            matches = matches[:MAX_GLOB_ENTRIES]
            matches.append(
                f"... (truncated: more than {MAX_GLOB_ENTRIES} entries match)"
            )
        if not matches:
            return "no matches", False
        return "\n".join(matches), False

    def _grep(self, arguments: Mapping[str, Any], root: Path) -> tuple[str, bool]:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return "pattern must be a non-empty string", True
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex: {exc}", True
        raw_path = arguments.get("path")
        target = self._jail(str(raw_path) if raw_path else ".", root)

        matches: list[str] = []
        truncated = False
        files: list[Path]
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = list(_walk_text_files(target))
        else:
            return f"no such file or directory: {raw_path!r}", True

        for file_path in files:
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        f"{_relative(file_path, root)}:{line_number}: {_cap(line, 500)}"
                    )
                    if len(matches) >= MAX_GREP_MATCHES:
                        truncated = True
                        break
            if truncated:
                break
        if not matches:
            return "no matches", False
        if truncated:
            matches.append(
                f"... (truncated: stopping at {MAX_GREP_MATCHES} matches)"
            )
        return "\n".join(matches), False

    # -- path jail ------------------------------------------------------
    @staticmethod
    def _jail(raw: str, root: Path) -> Path:
        """Resolve ``raw`` against ``root`` and refuse anything that escapes.

        ``Path.resolve`` follows symlinks, so a link inside the workdir that
        points outside is rejected exactly like a ``..`` path.
        """

        if not raw:
            raise _ToolError("path must be a non-empty string")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise _ToolError(f"cannot resolve path: {exc}") from exc
        if not _inside(resolved, root):
            raise _ToolError(JAIL_ERROR)
        return resolved

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            process.kill()
        except OSError:  # pragma: no cover - already gone
            pass


class _ToolError(Exception):
    """A tool argument/IO problem reported to the model as a tool error."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:  # pragma: no cover - jail guarantees containment
        return path.as_posix()


def _walk_text_files(root: Path):
    """Yield candidate files under ``root`` deterministically, skipping .git."""

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or path.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _normalize_content(response: Any) -> list[dict[str, Any]]:
    """Turn a Messages response body into a list of content blocks."""

    if not isinstance(response, dict):
        raise PlannerError("model response must be a JSON object")
    content = response.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise PlannerError("model response has no content")
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            blocks.append({"type": "text", "text": block})
        elif isinstance(block, dict):
            blocks.append(block)
    return blocks


def _text_of(content: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        str(block.get("text") or "")
        for block in content
        if block.get("type") in (None, "text")
    )


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (truncated, {len(text)} characters total)"


def _short_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - inputs are JSON already
        return repr(value)
