# Ongoing tasks and execution tickets

## Intent and references

The product goal is a self-hosted assistant that owns a user's piece of work
across chat turns and background execution. Grokbot and Dot are experience
references, not claims about their internal architectures.

The public [DeepSeek research prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)
is the implementation reference, specifically its
[domain model](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/001-domain.md)
and [role separation](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/004-prompts.md#principles-of-the-built-in-text).
Its main Session routes; the Executor investigates, implements and verifies.
Its Ticket outlives an individual Assignment. This project keeps that distinction
without importing its GitHub-specific subscription, authorization and effect systems.

## Two lifetimes

- **Task**: the user's ongoing request. Stores the exact original request,
  starting conversation/project context, later user messages, working directory,
  execution ticket ids and delivery outcome in the existing SQLite database.
- **Ticket**: one queue execution. Existing worker adapters, leases, approval
  and per-ticket review still own this lifecycle. A ticket finishing does not
  by itself complete the Task.

The names preserve the existing CLI/API. Our Task roughly corresponds to the
reference prototype's longer-lived Ticket; our execution Ticket is not the same
domain concept as its Ticket.

## Flow

1. Brain answers directly, routes a follow-up/status question to an existing
   Task, or opens a new Task. An explicit or unambiguous reference is required
   for follow-ups; mere topic similarity is insufficient.
2. Default to one coherent execution ticket. Split only genuinely independent
   deliverables. Investigation and implementation choices belong to the Worker.
3. The Worker gets the original request and constraints, later user messages,
   and prior execution reports. Dependencies additionally carry their results
   and artifact locations. Reports are leads to verify, not execution evidence.
4. Once that batch finishes, Brain checks delivery against the original request
   plus explicit user updates, using current artifact excerpts and ticket results.
   It returns `ready`, `continue`, `needs_input`, or `failed`.
5. `continue` names an unmet requirement or missing evidence and returns it to
   the same Worker adapter and directory. It does not prescribe a new technical
   DAG. `needs_input` asks one concrete question and launches nothing automatically.
6. Follow-ups reuse the Task. If execution is running, the message is retained
   and handed over after the current batch, rather than starting a competing edit.

The HTTP Brain coordinator progresses Tasks after `/chat` times out or a client
disconnects. It records the final response once in the original user's history.
The web client polls the Task, so all tickets reaching `done` no longer ends its
progress view before delivery is assessed, and restores recent Tasks on reload.
`/tasks?user_id=...` and `/tasks/<id>`
expose persisted outcomes without making another model call.

## Scope and costs

- One HTTP Brain coordinator per runtime directory; the separate queue daemon
  still runs workers. This is not a distributed coordinator implementation.
- Initial planning + delivery assessment normally uses the same two Brain calls
  as the former planning + summary flow. Each continuation adds one execution
  and one assessment. `brain_max_rounds` defaults to 3, including the first batch;
  an explicit user follow-up starts a fresh continuation budget.
- A planned batch is recorded before enqueueing; deterministic assignment ticket
  ids let a restart adopt an already-created ticket rather than recreate it.
  The daemon's existing interrupted-worker retries still have at-least-once
  side effects; this change does not promise exactly-once external actions.
- Task context survives restart, but this is not a persistent Claude/native
  model session. Follow-ups are delivered between execution batches, not injected
  into a running CLI process. Live steering/cancellation is not implemented here.
- Web task delivery and stored conversation outcomes are implemented. A final
  push to Feishu after its initial webhook reply, and task-level polling in the
  packaged Android/Windows clients, are not implemented in this change. Existing
  clients retain ticket polling and can ask the main agent for task status.
- Delivery assessment is a model judgement, not a proof. It can read only listed,
  size-bounded text artifacts and reported checks. It does not execute tests;
  missing runtime evidence must be obtained by the Worker. Artifact content goes
  to the configured Brain model as well as any optional per-ticket reviewer.

Tests use scripted models with real stores, workers, daemon and HTTP. They verify
the lifecycle and context plumbing, not whether the selected models judge goals
correctly or improve real-task completion rate.
