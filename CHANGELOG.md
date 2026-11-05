# Changelog

## Unreleased

- Persist user Tasks separately from execution tickets; preserve original
  intent, context and follow-ups across chat timeouts and server restarts.
- Keep technical decisions with workers and default to coarse-grained dispatch.
  Hand over original constraints and previous execution reports explicitly.
- Replace the final chat summary with goal-level delivery assessment; support
  same-task continuation and concrete requests for human input.
- Continue tasks in the HTTP coordinator, record final responses in conversation
  history, and expose task polling/restoration in the web client.
- Add 18 lifecycle/API regression tests and a scripted browser QA fixture.

## v0.6.0 — 2026-11-04
- **Streaming chat**: new `POST /chat/stream` (SSE). Clients see progress as it
  happens — accepted → planning → tickets → per-ticket status → final reply —
  instead of waiting silently for the whole task.
- Web UI, Android and Windows clients all consume the stream with a quiet
  live status line; automatic fallback to the classic `/chat` when streaming
  is unavailable.
- 301 tests.

## v0.5.0 — 2026-11-03
- Native worker: in-house agent loop with standard tools (read/write/edit/
  bash/glob/grep), workspace-jailed, model-swappable.
- Brain: per-user conversation thread memory; reply polish.
- Clients: Android + Windows (DSH-inspired design), error-message fixes.

## v0.4.0 — 2026-11-03
- Phase 5: semantic review loop (shadow/enforce), minimal approval gate,
  append-only decision log.

## v0.3.0 — 2026-11-03
- Web chat UI, artifact downloads, Feishu channel skeleton, systemd deploy.

## v0.2.0 — 2026-11-03
- Daemon + queue + worker pool; Brain planner with chat/HTTP entry.

## v0.1.0 — 2026-11-03
- Router/Worker skeleton, SQLite tickets, structured result contract.
