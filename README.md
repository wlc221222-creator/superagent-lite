# superagent-lite

A small, self-hosted **router/worker agent**: users talk to one main agent (the
router); the router turns each task into a **ticket**, dispatches it to an
execution agent (Claude Code, a test echo worker, later Codex), supervises the
run, and reports the result. Inspired by the Router/Worker two-layer design of
DeepSeek's Super-agent.

This is the MVP: standard library only, SQLite persistence, a single CLI.

## Architecture

```
                    ┌──────────────────────────────────────────────┐
  user ── task ───▶ │  Router (main agent)                         │
                    │  - understands the task, creates a Ticket    │
                    │  - picks a Worker, dispatches, supervises    │
                    │  - collects results, replies to the user     │
                    └───────────────┬──────────────────────────────┘
                                    │ ticket + isolated workdir
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
      ┌───────────────┐    ┌────────────────┐    ┌──────────────┐
      │ ClaudeCode    │    │ EchoWorker     │    │ CodexWorker  │
      │ Worker        │    │ (offline test) │    │ (placeholder)│
      │ claude -p ... │    │                │    │              │
      └───────┬───────┘    └────────────────┘    └──────────────┘
              │ subprocess (timeout, kill)
              ▼
      ~/.claude (its own auth / config)

  Tickets + status ──▶ SQLite: runtime/superagent.db
  Per-ticket logs  ──▶ runtime/logs/<ticket_id>.log
  Per-ticket cwd   ──▶ runtime/work/<ticket_id>/
```

If the process dies mid-ticket, the router marks leftover `running` tickets
`failed` ("interrupted") on its next startup, so the list never lies about a
ticket that is no longer executing.

A **Ticket** is the unit of work: `id`, `description`, `status`
(`pending` / `running` / `done` / `failed`), assigned worker, working
directory, timeout, result summary, error, exit code, full log path, and
timestamps. Tickets are persisted in SQLite, so a restart loses nothing.

The **Router** is the only public entry point (`Router.submit`, `get`/`result`,
`list`). Workers are adapters behind a tiny interface:

```python
class Worker(ABC):
    name: ClassVar[str]
    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult: ...
```

## Quick start

Requirements: Python 3.11+ (developed on 3.12), no third-party packages.

```bash
# optional: copy the example config
cp config.example.json config.json

# run a task offline with the echo worker
python -m superagent_lite run "写一个测试任务" --worker echo

# inspect history
python -m superagent_lite list
python -m superagent_lite show <ticket_id>
python -m superagent_lite show <ticket_id> --log

# run the smoke test (no pytest needed)
python tests/test_smoke.py
# or
python -m unittest discover -s tests
# or, if installed:
python -m pytest tests/
```

Everything lands under `runtime/` by default:

```
runtime/
├── superagent.db          # tickets
├── logs/<ticket_id>.log   # full stdout/stderr + lifecycle events
└── work/<ticket_id>/      # isolated cwd for each ticket
```

### CLI commands

| Command | Description |
| --- | --- |
| `python -m superagent_lite run "<task>" --worker echo\|claude\|codex` | Create a ticket, execute it, print the summary |
| `python -m superagent_lite list [--status done] [--limit 20] [--json]` | List tickets, newest first |
| `python -m superagent_lite show <ticket_id> [--log] [--json]` | Show one ticket and its result |

`python main.py ...` and the `superagent` console script (after
`pip install -e .`) are equivalent entry points. All commands accept
`--config PATH`.

### Configuration

The code reads `config.json` from the current directory; if it is missing,
built-in defaults are used.

```json
{
  "default_worker": "echo",
  "claude_binary": "claude",
  "claude_model": "claude-sonnet-5-5",
  "claude_extra_args": [],
  "timeout_seconds": 300,
  "runtime_dir": "runtime"
}
```

| Key | Meaning |
| --- | --- |
| `default_worker` | Worker used when `--worker` is omitted (`echo`, `claude`, `codex`) |
| `claude_binary` | Name/path of the Claude Code CLI |
| `claude_model` | Model passed as `--model`; `null` to use Claude Code's own default |
| `claude_extra_args` | Extra argv appended to the `claude` command |
| `timeout_seconds` | Per-ticket timeout (a hard kill of the process group) |
| `runtime_dir` | Base directory for the DB, logs and workdirs |

## Connecting Claude Code

1. Install and authenticate the Claude Code CLI so that `claude -p "hello"`
   works in your shell. superagent-lite never stores credentials: Claude Code
   uses its own `~/.claude/settings.json` (or environment) for auth and
   endpoint configuration.
2. Point the config at it if it is not on `PATH`:

   ```json
   { "default_worker": "claude", "claude_binary": "/usr/local/bin/claude",
     "claude_model": "claude-sonnet-5-5", "timeout_seconds": 600 }
   ```

3. Run:

   ```bash
   python -m superagent_lite run "Create hello.py that prints hello" --worker claude
   ```

Each ticket runs `claude -p "<task>" --model <model>` inside
`runtime/work/<ticket_id>`, with stdin closed. stdout/stderr and the exit code
are captured in `runtime/logs/<ticket_id>.log`; a timeout kills the whole
process group and marks the ticket `failed`. Run `show <id> --log` to audit a
run.

## Project layout

```
superagent_lite/
├── models.py            # Ticket + TicketStatus
├── store.py             # SQLite ticket store
├── router.py            # submit / dispatch / list / result
├── cli.py               # python -m superagent_lite
├── config.py            # JSON config loading + defaults
└── workers/
    ├── base.py          # Worker interface, WorkerResult
    ├── claude_code.py   # claude -p adapter (subprocess, timeout)
    ├── echo.py          # offline test worker
    └── codex.py         # placeholder
tests/test_smoke.py      # create → execute → complete → query
config.example.json
main.py                  # equivalent entry point
```

## Roadmap

- **Codex adapter** — implement `CodexWorker.run` against the Codex CLI.
- **HTTP API** — expose `submit` / `list` / `result` as a small service.
- **Messaging channels** — receive tasks and reply through chat platforms.
- **Watchdog & automatic retry** — liveness monitoring; the `retry_count` /
  `max_retries` fields are already persisted and reserved for this.
- **License** — TBD (to be chosen before open-sourcing).

## Out of scope for the MVP

Web UI, HTTP server, multi-user permissions, chat integrations, and a real
retry/watchdog implementation.
