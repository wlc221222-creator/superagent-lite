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

While a worker runs, the router refreshes the ticket's `heartbeat_at`. If the
process dies mid-ticket, the next startup marks every `running` ticket whose
heartbeat is older than `stale_seconds` as `failed` ("interrupted"), writes a
note to its log, and leaves genuinely live rows alone -- so the list never
lies about a ticket that is still executing.

A **Ticket** is the unit of work: `id`, `description`, `status`
(`pending` / `running` / `done` / `failed`), assigned worker, working
directory, timeout, result summary, `artifacts`, error, exit code, `attempts`,
`retry_count` / `max_retries`, `claim_token`, full log path, and timestamps
(`created_at`, `started_at`, `heartbeat_at`, `finished_at`). Tickets are
persisted in SQLite, so a restart loses nothing.

### superagentd: the resident daemon (phase 3, milestone A)

`run` executes a ticket synchronously. For a resident setup there is
`superagentd`: a foreground daemon that owns the queue and a worker pool.

```
     superagent queue "…"          superagent list / show
             │                             ▲
             ▼                             │
    ┌────────────────────────────────────────────────────┐
    │ superagentd                                        │
    │  main loop ─▶ claim (UPDATE … WHERE pending) ─┐    │
    │  watchdog ─▶ requeue stale running tickets    │    │
    │                    worker pool (N threads) ◀──┘    │
    │                    Router + result contract        │
    └────────────────────────────────────────────────────┘
```

- **Enqueue** (`superagent queue`) writes a `pending` ticket and returns; the
  submitting side never waits for execution.
- **Claiming** is a single conditional `UPDATE ... WHERE status='pending'`
  that also writes a fresh `claim_token`. Two daemons (or a daemon racing a
  synchronous `run`) cannot execute the same ticket twice.
- **Pool** -- `daemon_concurrency` tickets run at once (default 2). Workers
  are the same adapters as `run`, so the result contract and sandboxing are
  unchanged.
- **Heartbeats** -- each run refreshes `heartbeat_at` every
  `heartbeat_seconds`, guarded by its lease token.
- **Watchdog** -- every `daemon_watchdog_seconds` a running ticket whose
  heartbeat is older than `stale_seconds` is put back on the queue
  (`retry_count` + 1) or failed once `max_retries` is spent.
- **Restart recovery** -- an exclusive lock file
  (`runtime/superagentd.lock`) allows one daemon per runtime directory. At
  startup every `running` ticket is therefore an orphan of a dead process
  and is requeued before the pool starts; completed tickets are never
  re-run.
- **Graceful shutdown** -- SIGTERM/SIGINT stops new claims, gives in-flight
  runs `daemon_shutdown_grace_seconds` to finish, then requeues whatever is
  still running. A late result from an abandoned run carries a stale lease
  token and is discarded, so it cannot overwrite the requeued row.

A non-daemon `run`/`show`/`list` still recovers stale tickets, but it fails
them (iteration 2 behaviour) rather than requeueing them.

### The Brain: chat, planning and tickets (phase 3, milestone B)

Users talk only to the main agent. `superagent chat` (CLI REPL) and
`superagent serve` (HTTP) share one `Brain`, so both behave identically:

```
user sentence
      │
      ▼
  Brain ──▶ planner model (/v1/messages, cheap model by default)
      │
      ├── "reply"  ─────────────────────────────▶ direct answer, no ticket
      │
      └── "plan"   ──▶ 1..N tickets (depends_on) ──▶ SQLite queue
                                                       │
                                             superagentd executes them
                                                       │
                        structured results ◀───────────┘
                              │
                              ▼
                     summarize → one reply
```

- **Decomposition** -- the planner returns JSON: either
  `{"action": "reply", "reply": "..."}` or
  `{"action": "plan", "workdir": ..., "tickets": [{"id", "description",
  "worker", "depends_on"}]}`. Ticket descriptions must stand alone because a
  worker only sees its own description. All tickets of one request share a
  working directory (the absolute path the user named, else a private
  `runtime/work/brain_<stamp>/`), so later steps can use earlier output.
- **Dependencies** -- `depends_on` is persisted and enforced by the store: a
  ticket is only handed out once every prerequisite is `done`. A failed
  prerequisite fails its dependents (transitively, from every path that writes
  `failed`) instead of parking them forever. Independent tickets in a plan
  still run in parallel.
- **Waiting** -- `chat`/`POST /chat` poll the tickets for
  `chat_timeout_seconds`; if they are not all terminal in time, the reply
  lists the ticket ids (and their statuses) so the caller can ask again.
  `POST /chat/stream` runs the same chat and reports progress as server-sent
  events (`accepted` -> `planning` -> `tickets` -> `ticket` per status change
  -> `done`, or `error`), which is what the web page shows in its status line.
- **Summary** -- the planner model receives the workers' *structured* results
  (status, summary, artifacts, error as JSON) and writes the final answer. It
  is told not to claim success the ticket data does not show; a failed ticket
  is reported as failed.
- **Memory** -- `memory.md` (path configurable) is injected into the planner
  prompt, truncated to `brain_memory_max_chars` (default 2000, keeping the
  beginning of the file). It is maintained by hand; nothing is auto-distilled.
- **Errors** -- an unreachable/misconfigured planner comes back as
  `mode: "error"` in the reply (HTTP 502) rather than crashing the REPL.

#### Planner endpoint and credentials

The planner calls an Anthropic-compatible `POST /v1/messages` over
`urllib` -- standard library only. **No credential lives in this repo or in
`config.json`.** At request time `superagent_lite/planner.py` reads the
`env` block of `~/.claude/settings.json` (the same file Claude Code uses):

```json
{ "env": { "ANTHROPIC_BASE_URL": "https://gateway.example",
           "ANTHROPIC_AUTH_TOKEN": "..." } }
```

and falls back to the live process environment when a key is absent there.
`brain_settings_path` changes the file location. The token is only held in
memory for the request; error messages are redacted so it cannot reach a log
or a reply. Only the model name is configurable:

| Key | Meaning |
| --- | --- |
| `brain_model` | Planner model (default `cn:deepseek-v4.1-flash`) |
| `brain_advanced_model` | Optional stronger planner model; `null` = never escalate |
| `brain_escalate_on_complex` | Allow escalation when the planner replies `"complex": true` (off by default) |
| `brain_max_tickets` | Upper bound on tickets per plan (default 8) |
| `brain_max_tokens` / `brain_timeout_seconds` | Planner request bounds |
| `brain_worker` | Worker for tickets that do not name one (default `claude`) |
| `brain_settings_path` | Where the endpoint/token are read from |
| `memory_path` / `brain_memory_max_chars` | Memory file and its prompt budget |
| `chat_timeout_seconds` / `chat_poll_seconds` | How long `/chat` waits, and how often it polls |
| `http_host` / `http_port` | `serve` bind address (default `127.0.0.1:8765`) |

#### HTTP API

`python -m superagent_lite serve` (or `--host/--port`) starts
`ThreadingHTTPServer`:

| Request | Response |
| --- | --- |
| `GET /` | the single-file web chat UI (public; no external resources) |
| `POST /chat` `{"text": "...", "user_id": "..."}` | `{"reply", "mode", "ticket_ids", "timed_out", "model"}`; `mode` is `reply` / `summary` / `pending` (timeout) / `error` (502) |
| `POST /chat/stream` (same body/auth) | `text/event-stream` of `data: {json}` events: `accepted`, `planning`, `tickets`, `ticket` (status change), then `done` (same fields as `/chat`) or `error`; `: ping` comments every 15 s |
| `GET /tickets/<id>` | the ticket, including `status`, `artifacts`, `result_summary`, `depends_on` |
| `GET /tickets/<id>/artifacts` | `{"ticket_id", "artifacts": [{"name", "size", "exists", "url"}]}` |
| `GET /tickets/<id>/artifacts/<name>` | the artifact bytes, with a guessed `Content-Type` and `Content-Disposition: attachment` |
| `POST /channels/feishu` | Feishu event webhook (see below) |
| `GET /health` | liveness and queue counters (never authenticated) |

When `SUPERAGENT_HTTP_TOKEN` is set, every endpoint except `/health`, `GET /`
and the Feishu webhook requires `Authorization: Bearer <token>` (or
`X-SuperAgent-Token`); anything else is a 401 before the body is read,
comparisons use `hmac.compare_digest`. Unset, the server is open -- `serve`
warns if that happens on a non-loopback address. `GET /` is public so a
browser can load the page and type a token into it; the page's API calls are
authenticated like any other client's.

#### Web UI

Open `http://<host>:<port>/` and paste the token into the field at the top
right (it is kept in `sessionStorage`, never in the URL). The page posts your
message to `/chat`, prints the reply, and if the reply carries ticket ids it
polls `GET /tickets/<id>` to show each ticket's status, summary and artifact
links. Downloads go through `GET /tickets/<id>/artifacts/<name>` with the same
token. The whole page is one HTML document with inline CSS/JS in
`superagent_lite/webui.py` -- no CDN, no build step.

#### Artifact downloads

Only files **inside the ticket's working directory** are servable. The
requested name is resolved (symlinks included) and must remain under the
resolved workdir, so `../secret`, `%2e%2e%2fsecret`, absolute paths, symlinks
pointing outside and directories all return 404 -- the same answer as a file
that simply does not exist, so a client cannot probe the host filesystem.
`GET /tickets/<id>/artifacts` lists what the ticket *recorded*, and marks
entries that were deleted or never created with `"exists": false`.

```bash
SUPERAGENT_HTTP_TOKEN=... python -m superagent_lite serve --port 8765
curl -s localhost:8765/chat -H 'Authorization: Bearer ...' \
     -H 'Content-Type: application/json' \
     -d '{"text": "create /tmp/demo/hello.txt containing hi"}'
curl -s localhost:8765/tickets/<id> -H 'Authorization: Bearer ...'
curl -s localhost:8765/tickets/<id>/artifacts -H 'Authorization: Bearer ...'
curl -sOJ localhost:8765/tickets/<id>/artifacts/hello.txt -H 'Authorization: Bearer ...'
```

#### Feishu (Lark) channel

`POST /channels/feishu` is a skeleton for the Feishu event subscription API:

- **`url_verification`** -- Feishu's setup handshake; the payload's
  `challenge` is echoed back as `{"challenge": "..."}`.
- **`im.message.receive_v1`** with a text message -- the text is extracted and
  passed to the same `Brain` the web UI uses. If `FEISHU_APP_ID` and
  `FEISHU_APP_SECRET` are set, the reply is sent to the chat through
  `open.feishu.cn`; otherwise it is only written to the channel log
  (`runtime/channel.log` by default; override with `SUPERAGENT_CHANNEL_LOG`).
- Other events, non-text messages and messages sent by a bot are acknowledged
  with 200 and ignored. A brain or Feishu-API failure is logged and still
  acknowledged, because anything else makes Feishu re-deliver the event and
  run the task a second time.

The webhook authenticates with Feishu's own `token` field, not the static
bearer token (Feishu cannot send custom headers). Set
`FEISHU_VERIFICATION_TOKEN` to the verification token from the Feishu app
console and mismatching payloads are refused with 401.

**Feishu setup (you need your own app; nothing here creates or stores one):**

1. In the Feishu open platform ("开发者后台"), create a custom app
   (企业自建应用) and note its **App ID** and **App Secret**.
2. Enable the **bot** capability and add the permission
   `im:message` (receive messages) plus `im:message:send_as_bot` (send
   replies). Publish a version of the app for the tenant.
3. Under **Events & Callbacks** set the request URL to
   `https://<your-host>/channels/feishu` and add the event
   `im.message.receive_v1`. Feishu sends the `url_verification` challenge
   immediately -- the endpoint must already be reachable, which is why the
   recommended path is a reverse proxy or private network (below).
4. Copy the **Verification Token** from the same page and export the three
   variables (never commit them):

   ```bash
   FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx FEISHU_VERIFICATION_TOKEN=xxx \
     python -m superagent_lite serve --port 8765
   ```

   Under systemd put them in `/etc/superagent/superagent-http.env` (created by
   `deploy/install.sh`, mode 600).
5. Add the bot to the chat you want to talk from and send it a message.

Not implemented in this phase: Feishu's encrypted/signed request scheme
(`Encrypt Key`, `X-Lark-Signature`), rich-text/card replies, file uploads and
per-user session state. Until signature verification is added, do not expose
the webhook to the open internet.

#### Deployment and remote access (部署与远程访问)

The two long-running processes are the queue daemon and the HTTP API. Both run
from the project directory and read `./config.json` (or `--config PATH`).

Recommended topology: **daemon and HTTP on one host, reachable only over a
private network.**

```
  browser ──https──▶ reverse proxy (Caddy/nginx, TLS, auth) ──▶ 127.0.0.1:8765
  Feishu  ──https──▶ same proxy ────────────────────────────▶ 127.0.0.1:8765/channels/feishu
                                                                    │
                                              superagent-http (serve)│ enqueues
                                                                    ▼
                                              superagentd ──▶ claude / echo workers
```

**systemd**

```bash
# on the target host, from a checkout of this repo:
sudo ./deploy/install.sh                 # PREFIX=/opt/superagent-lite by default
#   SERVICE_USER=deploy PREFIX=/srv/superagent sudo -E ./deploy/install.sh
```

`install.sh` is idempotent: it copies the source, creates `config.json` only
if none exists, creates `/etc/superagent/superagentd.env` and
`/etc/superagent/superagent-http.env` (mode 600) only if absent, rewrites
`WorkingDirectory`/`ExecStart` to the install prefix, and then
`systemctl daemon-reload && systemctl enable --now`. It refuses to run as a
non-root user and checks for systemd and Python >= 3.11 first. `NO_START=1`
installs without starting.

Both units use `Restart=on-failure` and an optional `EnvironmentFile=-...`, so
a missing env file is not an error. The daemon needs no token; the HTTP unit
needs `SUPERAGENT_HTTP_TOKEN` in its env file as soon as it is reachable by
anyone but you.

```bash
systemctl status superagentd superagent-http
journalctl -u superagentd -f
```

**Bind address** -- set `http_host` / `http_port` in `config.json`
(`serve --host/--port` override them):

| `http_host` | Use |
| --- | --- |
| `127.0.0.1` (default) | local browser or a reverse proxy on the same host |
| a Tailscale/WireGuard address | direct access from your own devices |
| `0.0.0.0` | only behind a firewall **and** with `SUPERAGENT_HTTP_TOKEN` set |

**Remote access, in order of preference** (推荐：内网 / Tailscale / 反向代理 + token，
不要把服务裸奔在公网上):

1. **Private network (Tailscale / WireGuard / an SSH tunnel)** -- keep
   `http_host` on the tailnet address or on `127.0.0.1` and forward a local
   port: `ssh -L 8765:127.0.0.1:8765 user@host`. No public listener at all.
2. **Reverse proxy with TLS + auth** -- nginx/Caddy in front of
   `127.0.0.1:8765`, terminating HTTPS, adding basic auth or OIDC, and
   forwarding the `Authorization` header (or injecting the token).
   `/channels/feishu` needs a public HTTPS URL, so it is the one path that
   usually must be proxied; restrict it to Feishu's IP ranges if you can.
3. **Never bind `0.0.0.0` on the open internet.** Anyone who reaches `/chat`
   can run tasks -- file writes, shell commands -- as the service user. The
   static token is a shared secret with no rate limiting; treat it as
   defence in depth, not as the only lock. `serve` warns when it binds a
   non-loopback address without a token.

Guarantees, stated precisely: a ticket in a terminal state (`done` /
`failed`) is never executed again, and a queued ticket is not lost. An
**interrupted** run is retried, so a task that was half-way through when its
daemon died can run its side effects twice (at-least-once, like any queue
without task-level idempotency). If the daemon is SIGKILLed, the `claude`
child it started may outlive it for a moment; it can no longer write ticket
state (the lease token is gone) but it can still touch its workdir.

```bash
# terminal 1
python -m superagent_lite daemon --concurrency 2

# terminal 2
python -m superagent_lite queue "写一个测试任务" --worker echo
python -m superagent_lite show <ticket_id> --log
```

The daemon logs lifecycle lines to stdout and to `runtime/superagentd.log`.

### Result contract (who decides `done`)

A child process exiting with code 0 does **not** mean the task was done -- a
model can politely refuse and still exit 0. So every Claude task is sent with a
mandatory instruction appended (see `superagent_lite/result.py`) asking for a
final line:

```
RESULT {"status": "success", "summary": "created answer.txt", "artifacts": ["answer.txt"]}
```

(`status` is `"failed"` with an explanation when the task could not be done.)

The router reads the **last** `RESULT` line and decides:

| Worker output | Ticket |
| --- | --- |
| `status: success`, parseable | `done`; `artifacts` recorded on the ticket |
| `status: failed` | `failed`, `error` = the summary |
| no `RESULT` line / invalid JSON / unknown status | `failed` ("missing RESULT line ...") |
| timeout, binary not found, non-zero exit | `failed` with the adapter's precise error |

Artifacts are normalized to paths relative to the ticket's working directory
and are visible in `show` (and in `--json`). EchoWorker satisfies the same
contract by returning a `WorkerResult` directly.

The **Router** is the only public entry point (`Router.submit`, `get`/`result`,
`list`). Workers are adapters behind a tiny interface:

```python
class Worker(ABC):
    name: ClassVar[str]
    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult: ...
```

### Workers

| Worker | What it is |
| --- | --- |
| `echo` | Offline stub: echoes the task, writes nothing. Safe default. |
| `claude` | Shells out to the Claude Code CLI (`claude -p ...`). |
| `native` | **In-house agent loop.** No external CLI: it calls the Messages API itself, runs the tools (`read_file`, `write_file`, `edit_file`, `bash`, `glob`, `grep`) and applies the same `RESULT` contract. Model from `native_model`, endpoint from `native_settings_path` (defaults to `brain_settings_path`). |
| `codex` | Placeholder. |

The native worker is the self-contained option: it needs nothing installed
beyond Python and an Anthropic-compatible endpoint. Every tool path is jailed
to the ticket's working directory, `bash` runs with an allow-listed
environment and `native_bash_timeout_seconds`, and the loop stops after
`native_max_turns` model turns. Pick it with `--worker native` or
`"brain_worker": "native"`.

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

# run the tests (no pytest needed)
python -m unittest discover -s tests
# or one file at a time:
python tests/test_smoke.py
python tests/test_contract.py
python tests/test_daemon.py
python tests/test_brain.py
python tests/test_phase4.py
```

The Brain/HTTP tests never touch the network: they start a stub
`/v1/messages` server (`tests/support.py::StubPlanner`) and point the Brain at
it through a temporary settings file, exactly the path production uses.

Everything lands under `runtime/` by default:

```
runtime/
├── superagent.db          # tickets
├── logs/<ticket_id>.log   # full stdout/stderr + lifecycle events
├── work/<ticket_id>/      # isolated cwd for each ticket
├── superagentd.log        # daemon lifecycle log
├── channel.log            # Feishu channel events and replies
└── superagentd.lock       # one-daemon-per-runtime lock
```

### CLI commands

| Command | Description |
| --- | --- |
| `python -m superagent_lite run "<task>" --worker echo\|claude\|codex` | Create a ticket, execute it, print the summary |
| `python -m superagent_lite queue "<task>" [--worker …] [--json]` | Create a `pending` ticket without executing it |
| `python -m superagent_lite daemon [--concurrency N] [--poll S] [--max-retries N]` | Run superagentd in the foreground |
| `python -m superagent_lite chat [--timeout S] [--model M] [--memory PATH] [--user-id ID]` | Talk to the main agent (REPL; pipes work too) |
| `python -m superagent_lite serve [--host H] [--port P] [--timeout S]` | Run the minimal HTTP API |
| `python -m superagent_lite list [--status done] [--limit 20] [--json]` | List tickets, newest first |
| `python -m superagent_lite show <ticket_id> [--log] [--json]` | Show one ticket and its result |

```bash
# terminal 1: the queue
python -m superagent_lite daemon
# terminal 2: the main agent
python -m superagent_lite chat
you> 帮我在 /tmp/demo 建个 hello.txt 并写一行字
agent> 已完成：创建了 /tmp/demo/hello.txt，内容为「你好」…
```

`chat` and `serve` use the configured worker (`brain_worker`, default
`claude`) for execution; if the planner endpoint is unreachable the reply says
so instead of the process dying.

`python main.py ...` and the `superagent` console script (after
`pip install -e .`) are equivalent entry points. All commands accept
`--config PATH`, before or after the sub-command.

### Configuration

The code reads `config.json` from the current directory; if it is missing,
built-in defaults are used.

See `config.example.json` for the full set; the essentials are:

```json
{
  "default_worker": "echo",
  "claude_bin": null,
  "claude_binary": "claude",
  "claude_model": "claude-sonnet-5-5",
  "claude_allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "claude_max_turns": 25,
  "claude_env_passthrough": ["HOME", "PATH", "LANG", "TERM"],
  "native_model": "cn:deepseek-v4.1-flash",
  "native_max_turns": 25,
  "native_settings_path": "~/.claude/settings.json",
  "native_bash_timeout_seconds": 120,
  "timeout_seconds": 300,
  "stale_seconds": 300,
  "heartbeat_seconds": 15,
  "max_retries": 1,
  "daemon_concurrency": 2,
  "daemon_poll_seconds": 0.5,
  "daemon_watchdog_seconds": 5,
  "daemon_shutdown_grace_seconds": 30,
  "runtime_dir": "runtime",
  "brain_model": "cn:deepseek-v4.1-flash",
  "brain_worker": "claude",
  "memory_path": "memory.md",
  "chat_timeout_seconds": 120,
  "http_host": "127.0.0.1",
  "http_port": 8765
}
```

The planner's `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` deliberately do
**not** appear here: they are read at runtime from `~/.claude/settings.json`
(see above).

| Key | Meaning |
| --- | --- |
| `default_worker` | Worker used when `--worker` is omitted (`echo`, `claude`, `native`, `codex`) |
| `claude_bin` | Explicit path to the Claude Code executable; wins over discovery. `null` = auto |
| `claude_binary` | Name/path used for `PATH` and fallback discovery (default `claude`) |
| `claude_model` | Model passed as `--model`; `null` to use Claude Code's own default |
| `claude_extra_args` | Extra argv appended to the `claude` command |
| `claude_allowed_tools` | Tools passed via `--allowedTools`; nothing else is granted |
| `claude_max_turns` | `--max-turns` bound on the agent loop |
| `claude_permission_mode` | `--permission-mode` (default `acceptEdits`); `null` to omit |
| `claude_env_passthrough` | Allow-list of parent env vars forwarded to the child |
| `native_model` | Model name for the in-house native worker |
| `native_max_turns` | Model turns the native agent loop may use before it fails |
| `native_settings_path` | Settings file with the API endpoint; defaults to `brain_settings_path` |
| `native_bash_timeout_seconds` | Per-command timeout for the native worker's `bash` tool |
| `timeout_seconds` | Per-ticket timeout (a hard kill of the process group) |
| `stale_seconds` | A `running` ticket whose heartbeat is this old is stale (failed by the CLI, requeued by the daemon) |
| `heartbeat_seconds` | How often the router refreshes `heartbeat_at` during a run |
| `max_retries` | How many interruptions a ticket survives before it is failed for good |
| `daemon_concurrency` | Worker pool size (default 2) |
| `daemon_poll_seconds` | How often an idle daemon checks the queue |
| `daemon_watchdog_seconds` | How often the watchdog looks for stale running tickets |
| `daemon_shutdown_grace_seconds` | On SIGTERM, how long in-flight runs may finish before being requeued |
| `runtime_dir` | Base directory for the DB, logs and workdirs |

### Binary discovery

The Claude CLI is located in this order: `claude_bin` from config, then a
`PATH` lookup of `claude_binary`, then `~/.local/bin/claude`. If none exists
the ticket fails with an explicit "claude binary not found" error naming
`claude_bin`. This means a `claude` installed only in `~/.local/bin` works
even when the orchestrator's `PATH` does not include it.

### Safety boundary

Remote task text reaches an agent with local file and shell access, so the
Claude adapter is constrained:

- **Tool allow-list** -- only `claude_allowed_tools` are granted
  (`--allowedTools`); everything else is denied.
- **Workspace** -- `--add-dir <ticket workdir>`; each ticket gets its own
  `runtime/work/<ticket_id>/` directory and runs there.
- **Bounded loop** -- `--max-turns`, plus the hard `timeout_seconds`.
- **Environment isolation** -- the child does not inherit the parent
  environment. Only `claude_env_passthrough` variables are copied (plus
  `PATH`); credential-looking variables are never forwarded.
- **Log redaction** -- before anything is written, values of `*_KEY`,
  `*_TOKEN`, `*_SECRET*` environment variables and `sk-...` strings are
  replaced with `***`.

There is no OS-level sandbox yet: an allowed `Bash` tool can still reach
outside the workdir if the task actively asks it to. Treat the worker as
semi-trusted and run it as a user with appropriate filesystem permissions.

## Connecting Claude Code

1. Install and authenticate the Claude Code CLI so that `claude -p "hello"`
   works in your shell. superagent-lite never stores credentials: Claude Code
   uses its own `~/.claude/settings.json` (or environment) for auth and
   endpoint configuration.
2. Point the config at it if it is not on `PATH` (installs under
   `~/.local/bin` are found automatically):

   ```json
   { "default_worker": "claude", "claude_bin": "/usr/local/bin/claude",
     "claude_model": "claude-sonnet-5-5", "timeout_seconds": 600 }
   ```

   Note the child gets only `claude_env_passthrough` variables, not your whole
   shell environment. If Claude Code authenticates through an environment
   variable (for example a gateway `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL`
   instead of `~/.claude/settings.json`), add that variable name to
   `claude_env_passthrough`; otherwise the run fails with a clear auth error.

3. Run:

   ```bash
   python -m superagent_lite run "Create hello.py that prints hello" --worker claude
   ```

Each ticket runs roughly

```
claude -p "<task + RESULT contract>" \
  --allowedTools Read,Write,Edit,Bash,Glob,Grep \
  --add-dir runtime/work/<ticket_id> --max-turns 25 \
  --permission-mode acceptEdits --model <model>
```

inside `runtime/work/<ticket_id>`, with stdin closed and a filtered
environment. stdout/stderr and the exit code are captured (redacted) in
`runtime/logs/<ticket_id>.log`; a timeout kills the whole process group and
marks the ticket `failed`. Run `show <id> --log` to audit a run.

## Project layout

```
superagent_lite/
├── models.py            # Ticket + TicketStatus
├── store.py             # SQLite queue: atomic claim, leases, requeue, recovery, depends_on
├── router.py            # enqueue / dispatch / heartbeat / result judgement
├── daemon.py            # superagentd: pool, watchdog, graceful shutdown
├── brain.py             # planner: reply-or-decompose, enqueue, wait, summarize
├── planner.py           # /v1/messages client; endpoint read from ~/.claude/settings.json
├── memory.py            # memory.md loading + truncation
├── http_api.py          # /chat, /tickets, artifact download, Feishu webhook, auth
├── webui.py             # single-file HTML chat page served at GET /
├── artifacts.py         # workdir-confined artifact listing / resolution
├── channels.py          # Feishu event parsing, url_verification, outbound API
├── result.py            # RESULT contract: prompt, parsing, verdict
├── redact.py            # secret redaction for logs
├── logs.py              # redacting log writer
├── cli.py               # python -m superagent_lite
├── config.py            # JSON config loading + defaults + binary discovery
└── workers/
    ├── base.py          # Worker interface, WorkerResult, env allow-list
    ├── claude_code.py   # claude -p adapter (tools, env, timeout)
    ├── native.py        # in-house agent loop over the Messages API
    ├── echo.py          # offline test worker
    └── codex.py         # placeholder
tests/
├── test_smoke.py        # create → execute → complete → query
├── test_contract.py     # contract, boundary, discovery, heartbeat tests
├── test_daemon.py       # queue → daemon → done, restart, pool, shutdown
├── test_brain.py        # planner stub, chat, HTTP API, memory, depends_on
├── test_phase4.py       # web UI, artifact download/traversal, Feishu, deploy assets
├── test_native_worker.py  # scripted-model native loop, tools and path jail
└── support.py           # stub claude CLI + stub /v1/messages planner
deploy/
├── superagentd.service  # systemd unit for the queue daemon
├── superagent-http.service  # systemd unit for the HTTP API + web UI
└── install.sh           # idempotent installer for both units
config.example.json
memory.md                # hand-maintained planner memory (template)
main.py                  # equivalent entry point
```

## Roadmap

- **Codex adapter** — implement `CodexWorker.run` against the Codex CLI.
- **Messaging channels** — Feishu is a skeleton; add signed-request
  verification, more event types and other platforms (Slack, Telegram).
- **Web UI** — the page is functional but minimal: no conversation history,
  no per-user sessions, no artifact preview.
- **Automatic memory distillation** — write `memory.md` from finished chats.
- **Multi-daemon** — claims and leases are already atomic; a coordinator or
  a shared watchdog would let several hosts drain one queue.
- **License** — TBD (to be chosen before open-sourcing).

## Out of scope for phase 3

Web UI, multi-user permissions and per-user isolation, automatic memory
distillation, and DAG-aware scheduling beyond `depends_on` gating.

## Out of scope for phase 4

Multi-user login/sessions in the web UI, encrypted Feishu callbacks, other
messaging channels, artifact previews, and containerised deployment (the
units assume a plain host install).

## Phase 5: semantic review, approval gate, decision log

Phase 5 adds a governance layer on top of the contract acceptance from
earlier phases. Everything is off or passive by default; nothing changes
unless you configure it.

**Semantic review.** After a ticket passes contract acceptance, a reviewer
model can check the actual artifacts against the ticket description (the
acceptance standard). The reviewer sees only the description, contract
facts, and the artifact contents (delimited as data, redacted for common
secret patterns first) — never the worker's own summary. Every non-`echo`
ticket is reviewed when review is on. A `reject` verdict sends the ticket
back once with the reviewer's reasons appended; a second reject fails it.
Review runs in three modes (`review_mode`): `off`, `shadow` (verdicts are
computed and logged, never block), and `enforce`. Roll out in shadow first.
Note that review sends artifact content to the configured model provider.

Key config: `review_mode` (default `off`), `review_model` (no default;
review stays off without one — pick a different model from the planner's),
`review_timeout_seconds` (default 60, must stay below `stale_seconds`).

**Approval gate.** Tickets whose description matches deterministic
patterns for irreversible or external actions (delete, publish, send,
pay, `git push`, …) start in `awaiting_approval` and are never claimed by
the daemon. Approve or reject them with `superagent-lite approve <id>` /
`reject <id>` or `POST /tickets/<id>/approve|reject`. Approval is bound to
the ticket's content: if the description changes after gating, the old
approval does not apply. This gate is a confirmation/reminder layer, not
a security boundary — an approved ticket's worker still runs with its
normal tool allowlist.

**Decision log.** `<runtime_dir>/decisions.jsonl` is an append-only JSONL
record of ticket creation, approval events, review verdicts, and final
outcomes, with a simple hash link between consecutive records. It exists
so decisions can be reviewed after the fact; write failures are counted
and logged, never block ticket processing.

**Review eval corpus.** `tests/fixtures/review_corpus/` holds labelled
cases (clean, defective, and prompt-injection cases). Grade candidate
reviewer models offline before trusting one:

```bash
python3 tools/run_review_eval.py --models <model-a> <model-b>
```

Ground truth in the corpus is fixed by construction — labels are written
when the cases are authored, never from a model's output. Grow the corpus
from real tickets during shadow operation, especially any case a reviewer
got wrong.
