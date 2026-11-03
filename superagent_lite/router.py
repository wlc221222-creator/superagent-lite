"""Router: the single entry point that creates, dispatches and supervises tickets."""

from __future__ import annotations

import inspect
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import default_config, normalize_config, resolve_claude_binary
from .decision_log import DecisionLog, open_decision_log
from .logs import append_log
from .models import (
    REVIEW_INCONCLUSIVE,
    REVIEW_NONE,
    REVIEW_PASSED,
    REVIEW_REJECTED,
    REVIEW_REJECTED_ONCE,
    Ticket,
    TicketStatus,
    new_ticket_id,
    utc_now,
)
from .result import ContractOutcome, check_contract, normalize_artifact
from .reviewer import (
    REASON_DISABLED,
    REASON_TIMEOUT,
    ReviewVerdict,
    Reviewer,
    sanitize_feedback,
    selection_kind,
    should_review,
)
from .store import TicketStore, approval_content_hash
from .workers import (
    ClaudeCodeWorker,
    CodexWorker,
    EchoWorker,
    NativeWorker,
    Worker,
    WorkerResult,
)

MAX_SUMMARY_CHARS = 4000
MAX_ARTIFACTS = 1000

#: Origins a ticket may carry; anything else is recorded verbatim but the
#: planner and gate only care about ``plan``.
KNOWN_ORIGINS = ("cli", "plan", "api", "feishu", "legacy")


class Router:
    """Main agent facade used by the CLI (and later by an HTTP API).

    The router owns the lifecycle of a ticket:

    ``submit`` -> create row (pending) -> ``dispatch`` (running, heartbeat) ->
    worker result -> contract judgement -> done/failed -> persisted result,
    artifacts and log.

    A worker that declares ``result_contract`` is judged by its last ``RESULT``
    line, never by its exit code alone.  A missing or malformed line is a
    failure.  Timeout handling lives in the adapters (the Claude adapter kills
    the process group); the router supplies the timeout and records failures.
    ``retry_count`` / ``max_retries`` are persisted but not yet acted upon.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        store: TicketStore | str | Path | None = None,
        workers: Mapping[str, Worker] | None = None,
        *,
        recover_mode: str = "fail",
        review_transport: Any = None,
        decision_log: DecisionLog | None = None,
    ) -> None:
        self.config = normalize_config(dict(config)) if config is not None else default_config()
        if recover_mode not in ("fail", "requeue", "none"):
            raise ValueError("recover_mode must be 'fail', 'requeue' or 'none'")
        self.recover_mode = recover_mode

        self.runtime_dir = Path(self.config["runtime_dir"]).expanduser().resolve()
        self.work_dir = self.runtime_dir / "work"
        self.log_dir = self.runtime_dir / "logs"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Phase 5: config is normalized once at startup, so every warning it
        # raised (review disabled for lack of a model, timeout clamped) is
        # available to the CLI/daemon to print loudly.
        self.warnings: list[str] = list(self.config.get("_warnings") or [])
        self.decision_log = (
            decision_log
            if decision_log is not None
            else open_decision_log(
                self.runtime_dir,
                enabled=bool(self.config.get("decision_log_enabled", True)),
            )
        )
        # The review call goes through the same settings-file endpoint as the
        # Brain, but through its own client so its timeout is the (shorter)
        # ``review_timeout_seconds``.  Tests inject a fake transport.
        self._review_transport = review_transport
        self.reviewer = self._build_reviewer()

        self._owns_store = store is None or isinstance(store, (str, Path))
        if store is None:
            store = self.runtime_dir / "superagent.db"
        if isinstance(store, (str, Path)):
            # Recovery is run explicitly below in the router's mode.
            store = TicketStore(
                store,
                stale_seconds=float(self.config["stale_seconds"]),
                recover=False,
            )
        self.store = store
        # A crashed previous process may have left tickets "running".  The
        # synchronous CLI fails them (iteration 2 behaviour); the daemon puts
        # them back on the queue so its pool re-executes them.  The Brain uses
        # "none": it only submits and observes, and must not mutate tickets
        # that a live daemon owns.
        self.recovered: list[Ticket] = []
        if self.recover_mode != "none":
            self.recovered = self.store.recover_stale_running(
                stale_seconds=float(self.config["stale_seconds"]),
                requeue=self.recover_mode == "requeue",
            )
            # Recovery may have failed a ticket that others depend on (retry
            # budget spent).  Those dependents can never run, so fail them now
            # instead of leaving them pending forever.
            for ticket in self.recovered:
                if ticket.status is TicketStatus.FAILED:
                    self.fail_dependents(ticket.id)

        claude_binary = resolve_claude_binary(self.config)
        default_workers: dict[str, Worker] = {
            "echo": EchoWorker(),
            "claude": ClaudeCodeWorker(
                model=self.config.get("claude_model"),
                binary=claude_binary,
                extra_args=self.config.get("claude_extra_args") or [],
                allowed_tools=self.config.get("claude_allowed_tools"),
                max_turns=self.config.get("claude_max_turns", 25),
                permission_mode=self.config.get("claude_permission_mode", "acceptEdits"),
                env_passthrough=self.config.get("claude_env_passthrough"),
                heartbeat_interval=float(self.config.get("heartbeat_seconds", 15.0)),
            ),
            # In-house agent loop: drives the model directly instead of
            # shelling out to an external coding-agent CLI.  It builds its
            # client lazily, so a missing endpoint fails a ticket, not startup.
            "native": NativeWorker(
                model=self.config.get("native_model"),
                settings_path=self.config.get("native_settings_path")
                or self.config["brain_settings_path"],
                max_turns=int(self.config.get("native_max_turns", 25)),
                bash_timeout_seconds=float(
                    self.config.get("native_bash_timeout_seconds", 120.0)
                ),
                request_timeout=float(self.config.get("timeout_seconds", 300.0)),
                heartbeat_interval=float(self.config.get("heartbeat_seconds", 15.0)),
            ),
            "codex": CodexWorker(),
        }
        if workers:
            default_workers.update(workers)
        self.workers = default_workers

    # -- public API -----------------------------------------------------
    def enqueue(
        self,
        text: str,
        worker: str | Worker | None = None,
        *,
        depends_on: Iterable[str] | None = None,
        workdir: str | Path | None = None,
        origin: str = "cli",
    ) -> Ticket:
        """Create a ticket and return it immediately.

        The ticket is persisted for a daemon (or a later ``dispatch``) to
        claim; nothing is executed here.  This is what ``superagent queue``
        uses so the submitting side never waits for the run.

        ``depends_on`` lists tickets that must be ``done`` first; the store
        will not hand the ticket out before then.  ``workdir`` overrides the
        default isolated directory, which is how the Brain points every
        ticket of one request at the same user-specified directory.

        The deterministic approval gate runs here, at creation (any origin):
        a description matching ``gate_patterns`` starts in
        ``awaiting_approval`` and records its intent in the decision log
        before the state is committed.
        """

        description = (text or "").strip()
        if not description:
            raise ValueError("task description must not be empty")

        adapter = self._resolve_worker(worker)
        ticket_id = new_ticket_id()
        dependencies = [str(item) for item in (depends_on or []) if str(item)]
        gated = self.is_gated(description)
        ticket = Ticket(
            id=ticket_id,
            description=description,
            worker=adapter.name,
            status=(
                TicketStatus.AWAITING_APPROVAL if gated else TicketStatus.PENDING
            ),
            timeout=float(self.config["timeout_seconds"]),
            workdir=str(
                Path(workdir).expanduser().resolve() if workdir is not None
                else self.work_dir / ticket_id
            ),
            log_path=str(self.log_dir / f"{ticket_id}.log"),
            max_retries=int(self.config["max_retries"]),
            depends_on=dependencies,
            origin=str(origin or "cli"),
            review_status=REVIEW_NONE,
            approval_hash=approval_content_hash(description) if gated else None,
        )

        Path(ticket.workdir).mkdir(parents=True, exist_ok=True)
        self.store.create(ticket)
        self.log_event(
            "ticket_created",
            ticket_id=ticket.id,
            details={
                "origin": ticket.origin,
                "worker": ticket.worker,
                "gated": gated,
                "depends_on": dependencies,
            },
        )
        if gated:
            # Intent before transition is the natural order here: the row was
            # created gated and the request record follows immediately.
            self.log_event(
                "approval_requested",
                ticket_id=ticket.id,
                details={
                    "description": ticket.description,
                    "approval_hash": ticket.approval_hash,
                },
            )
        append_log(
            ticket.log_path,
            "\n".join(
                [
                    f"[{ticket.created_at}] ticket created: {ticket.id}",
                    f"task: {ticket.description}",
                    f"worker: {ticket.worker}",
                    f"origin: {ticket.origin}",
                    f"workdir: {ticket.workdir}",
                    f"timeout: {ticket.timeout:g}s",
                    (
                        "[router] approval gate matched: awaiting_approval "
                        "(approve/reject required before it runs)"
                        if gated
                        else "[router] queued (pending); awaiting a daemon"
                    ),
                ]
            )
            + "\n",
        )
        return ticket

    def submit(self, text: str, worker: str | Worker | None = None) -> Ticket:
        """Create a ticket and dispatch it to a worker synchronously."""

        ticket = self.enqueue(text, worker=worker)
        return self.dispatch(ticket.id, worker=worker)

    def dispatch(self, ticket_id: str, worker: str | Worker | None = None) -> Ticket:
        """Run an existing pending ticket through its worker.

        The pending -> running transition is an atomic claim, so a daemon and
        a synchronous ``submit`` can never execute the same ticket twice.
        Every later write is conditional on the lease token claimed here.
        """

        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")

        adapter = self._resolve_worker(worker if worker is not None else ticket.worker)
        ticket = self.store.claim(ticket_id)
        if ticket is None:
            # Already running, done, or claimed by a daemon.  Return the
            # current row untouched rather than executing it a second time.
            return self.store.get(ticket_id)  # type: ignore[return-value]

        append_log(
            ticket.log_path,
            f"[{ticket.started_at}] started with {adapter.name} "
            f"(attempt {ticket.attempts})\n",
        )
        return self.run_claimed(adapter, ticket)

    def run_claimed(self, adapter: Worker, ticket: Ticket) -> Ticket:
        """Execute a ticket whose lease this process already holds.

        Shared by the synchronous :meth:`dispatch` and the daemon worker pool:
        both pass a ticket that ``TicketStore.claim*`` returned running and
        carrying a ``claim_token``.  The token guards every write, so if the
        lease is lost mid-run (watchdog requeue) the late result is discarded
        instead of clobbering the ticket.
        """

        token = ticket.claim_token
        append_log(
            ticket.log_path,
            f"[{utc_now()}] [router] claimed lease {token} "
            f"(worker={adapter.name})\n",
        )
        started = time.monotonic()
        heartbeat = self._make_heartbeat(ticket, token)
        heartbeat.start()
        try:
            result = self._run_worker(
                adapter, ticket, Path(ticket.workdir), heartbeat
            )
        except KeyboardInterrupt:
            heartbeat.stop()
            self._fail(ticket.id, "interrupted by user", started, token=token)
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure must be recorded
            heartbeat.stop()
            self._fail(
                ticket.id, f"{type(exc).__name__}: {exc}", started, token=token
            )
            if isinstance(exc, SystemExit):
                raise
            return self.store.get(ticket.id)  # type: ignore[return-value]

        # The heartbeat keeps ticking into the review (Revision 2 §2): the
        # review is bounded by review_timeout_seconds < stale_seconds, so the
        # lease can never expire merely because a reviewer is slow.  The
        # review resolves while the ticket is still ``running``; the ticket is
        # never marked ``done`` before it has.
        try:
            duration = time.monotonic() - started
            finished_at = utc_now()
            outcome = self._judge(adapter, result, Path(ticket.workdir))
            summary = _clip(outcome.summary or result.summary, MAX_SUMMARY_CHARS)

            if outcome.success:
                decision = self._review_decision(ticket, outcome, heartbeat)
            else:
                # A run that already failed its contract is not reviewed: the
                # review exists to question *success*, and the failure is
                # already precise.  The skip is logged so the decision log has
                # no silent gaps.
                self._log_review_skipped(ticket, "execution_failed")
                decision = ReviewDecision(
                    status=TicketStatus.FAILED,
                    error=_clip(
                        outcome.error or "worker reported failure", MAX_SUMMARY_CHARS
                    ),
                )

            requeued = decision.status is TicketStatus.PENDING
            finished_at_value = None if requeued else finished_at
            review_fields: dict[str, Any] = {
                "review_status": decision.review_status,
                "review_reasons": decision.reasons,
            }
            if decision.review_attempts is not None:
                review_fields["review_attempts"] = decision.review_attempts
            updated = self.store.set_status(
                ticket.id,
                decision.status,
                expected_claim_token=token,
                result_summary=summary or None,
                artifacts=outcome.artifacts[:MAX_ARTIFACTS],
                error=decision.error,
                exit_code=result.exit_code,
                description=decision.description or ticket.description,
                claim_token=None if requeued else token,
                started_at=None if requeued else ticket.started_at,
                heartbeat_at=None if requeued else ticket.heartbeat_at,
                finished_at=finished_at_value,
                **review_fields,
            )
        finally:
            heartbeat.stop()

        if updated is None:
            append_log(
                ticket.log_path,
                f"[{finished_at}] [router] lease {token} was lost; discarding "
                f"late result (success={outcome.success})\n",
            )
            return self.store.get(ticket.id)  # type: ignore[return-value]

        append_log(
            updated.log_path,
            "\n".join(
                [
                    f"[{finished_at}] finished status={updated.status.value} "
                    f"elapsed={duration:.2f}s",
                    f"[router] artifacts={len(updated.artifacts)}"
                    + (f" {updated.artifacts}" if updated.artifacts else ""),
                ]
            )
            + "\n",
        )
        if decision.status is TicketStatus.PENDING:
            # An enforce-mode review reject put the ticket back on the queue
            # with reviewer feedback appended.  It is not finished; nothing
            # downstream (dependents, approval gate) is touched.
            append_log(
                updated.log_path,
                f"[{finished_at}] [review] requeued for attempt "
                f"{decision.review_attempts}: {decision.reasons}\n",
            )
            return updated
        self.log_event(
            "ticket_finished",
            ticket_id=updated.id,
            details={
                "status": updated.status.value,
                "review_status": updated.review_status,
                "worker": updated.worker,
                "origin": updated.origin,
                "elapsed_seconds": round(duration, 3),
                "error": updated.error,
            },
        )
        if updated.status is TicketStatus.FAILED:
            self.fail_dependents(updated.id)
        return updated

    # -- phase 5: decision log -----------------------------------------
    def log_event(
        self,
        event: str,
        *,
        ticket_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a decision-log event; never raises, never blocks."""

        try:
            self.decision_log.append(event, ticket_id=ticket_id, details=details)
        except Exception:  # pragma: no cover - DecisionLog already swallows
            pass

    # -- phase 5: approval gate ----------------------------------------
    def is_gated(self, description: str) -> bool:
        """True when the description matches a configured gate pattern."""

        if not self.config.get("gate_enabled", True):
            return False
        for pattern in self.config.get("gate_patterns") or []:
            try:
                if re.search(pattern, description, re.IGNORECASE):
                    return True
            except re.error:  # pragma: no cover - validated at startup
                continue
        return False

    def approve(self, ticket_id: str) -> tuple[Ticket | None, str]:
        """Approve a gated ticket (``awaiting_approval`` -> ``pending``)."""

        ticket, error = self.store.approve(ticket_id)
        if ticket is None:
            return None, error
        self.log_event(
            "approval_granted",
            ticket_id=ticket.id,
            details={"approval_hash": approval_content_hash(ticket.description)},
        )
        append_log(
            ticket.log_path,
            f"[{utc_now()}] [router] approved by operator (pending)\n",
        )
        return ticket, ""

    def reject(self, ticket_id: str, *, reason: str = "rejected by approver") -> tuple[Ticket | None, str]:
        """Reject a gated ticket (``awaiting_approval`` -> ``failed``)."""

        ticket, error = self.store.reject(ticket_id, reason=reason)
        if ticket is None:
            return None, error
        self.log_event(
            "approval_rejected",
            ticket_id=ticket.id,
            details={"reason": reason},
        )
        append_log(ticket.log_path, f"[{utc_now()}] [router] {reason}\n")
        self.log_event(
            "ticket_finished",
            ticket_id=ticket.id,
            details={"status": ticket.status.value, "review_status": REVIEW_NONE},
        )
        self.fail_dependents(ticket.id)
        return ticket, ""

    # -- phase 5: semantic review --------------------------------------
    def _build_reviewer(self) -> Reviewer | None:
        """Construct the reviewer, or ``None`` when review is inactive.

        Review is inactive when ``review_mode`` is ``off`` or no
        ``review_model`` is configured (``normalize_config`` has already logged
        the latter).  The transport is the same Messages wire protocol as the
        Brain, but its own client with the shorter ``review_timeout_seconds``;
        unit tests inject a fake transport instead.
        """

        mode = str(self.config.get("review_mode", "off"))
        model = self.config.get("review_model")
        if mode == "off" or not model:
            return None
        transport = self._review_transport
        if transport is None:
            from .planner import MessagesClient, PlannerError, load_endpoint

            try:
                endpoint = load_endpoint(self.config["brain_settings_path"])
            except PlannerError as exc:
                # Same soft-failure policy as config normalization: review goes
                # off with a loud line, tickets keep flowing.
                self.warnings.append(f"review disabled: {exc}")
                self.config["review_mode"] = "off"
                return None
            transport = MessagesClient(
                endpoint, timeout=float(self.config["review_timeout_seconds"])
            )
        return Reviewer(
            str(model),
            transport,
            max_artifact_bytes=int(self.config["review_max_artifact_bytes"]),
            max_total_bytes=int(self.config["review_max_total_bytes"]),
            timeout=float(self.config["review_timeout_seconds"]),
            max_tokens=int(self.config["review_max_tokens"]),
            env=None,
        )

    def _log_review_skipped(self, ticket: Ticket, reason: str) -> None:
        """Record why a completed ticket never reached a reviewer.

        Invariant (Revision 2 §10): every completed non-echo ticket carries
        either a ``review_verdict`` or a ``review_skipped`` event.
        """

        self.log_event(
            "review_skipped",
            ticket_id=ticket.id,
            details={
                "reason": reason,
                "worker": ticket.worker,
                "review_mode": self.config.get("review_mode"),
            },
        )

    def _review_decision(
        self, ticket: Ticket, outcome: ContractOutcome, heartbeat: "_Heartbeat"
    ) -> "ReviewDecision":
        """Run the semantic review for a contract-accepted ticket and decide.

        Called while the ticket is still ``running`` and its heartbeat is
        live.  Returns the status/error/review fields the completion path must
        persist.  The policy:

        * ``shadow`` -- a verdict is computed and logged, never acted upon; the
          ticket ends ``done`` whatever the reviewer said.
        * ``enforce`` -- an explicit ``reject`` sends the ticket back for one
          more attempt (description + sanitized feedback) and then fails it;
          a ``pass`` completes it.
        * ``inconclusive`` -- never blocks: the ticket completes on its
          contract result and the separate status is logged and counted
          (Revision 4).
        """

        mode = str(self.config.get("review_mode", "off"))
        if self.reviewer is None or not should_review(ticket, mode=mode):
            self._log_review_skipped(
                ticket,
                "worker=echo" if ticket.worker == "echo" else REASON_DISABLED,
            )
            return ReviewDecision(status=TicketStatus.DONE)

        verdict, meta = self._run_reviewer(ticket, outcome, heartbeat)
        if verdict is None:
            return ReviewDecision(
                status=TicketStatus.DONE, review_status=REVIEW_INCONCLUSIVE
            )

        selection = selection_kind(ticket)
        self.log_event(
            "review_verdict",
            ticket_id=ticket.id,
            details={
                "verdict": verdict.verdict,
                "mode": mode,
                "selection": selection,
                "reasons": verdict.reasons,
                "evidence": verdict.evidence[:10],
                "raw": _clip(verdict.raw, 2000),
                "error": verdict.error,
                "model": meta.get("model"),
                "prompt_template_version": meta.get("prompt_template_version"),
                "redaction_rule_version": meta.get("redaction_rule_version"),
                "redaction_hits": meta.get("redaction_hits"),
                "artifact_bytes": meta.get("artifact_bytes"),
                "model_called": meta.get("model_called"),
            },
        )
        append_log(
            ticket.log_path,
            f"[{utc_now()}] [review] mode={mode} selection={selection} "
            f"verdict={verdict.verdict}"
            + (f" reasons={verdict.reasons}" if verdict.reasons else "")
            + (f" error={verdict.error}" if verdict.error else "")
            + "\n",
        )
        if meta.get("model_called") and meta.get("prompt"):
            # The prompt goes to the ticket log (redacted by append_log), never
            # to the decision log: that keeps one hash-chained verdict line.
            append_log(
                ticket.log_path,
                f"[{utc_now()}] [review] sent prompt:\n{meta['prompt']}\n",
            )

        if verdict.verdict == "inconclusive":
            self.log_event(
                "review_inconclusive",
                ticket_id=ticket.id,
                details={"reason": verdict.error or REASON_TIMEOUT, "mode": mode},
            )
            return ReviewDecision(
                status=TicketStatus.DONE, review_status=REVIEW_INCONCLUSIVE
            )

        if verdict.verdict == "pass":
            return ReviewDecision(
                status=TicketStatus.DONE, review_status=REVIEW_PASSED
            )

        # -- explicit reject -------------------------------------------
        if mode != "enforce":
            # Shadow: record, never block (Revision 4).
            self.log_event(
                "review_shadow_reject",
                ticket_id=ticket.id,
                details={"reasons": verdict.reasons, "mode": mode},
            )
            return ReviewDecision(
                status=TicketStatus.DONE, review_status=REVIEW_REJECTED
            )

        max_retries = int(self.config["review_max_retries"])
        if ticket.review_attempts < max_retries:
            description = ticket.description + sanitize_feedback(
                verdict.reasons,
                max_chars=int(self.config["review_max_feedback_chars"]),
            )
            self.log_event(
                "review_retry",
                ticket_id=ticket.id,
                details={
                    "reasons": verdict.reasons,
                    "review_attempt": ticket.review_attempts + 1,
                    "max": max_retries,
                },
            )
            return ReviewDecision(
                status=TicketStatus.PENDING,
                review_status=REVIEW_REJECTED_ONCE,
                reasons=list(verdict.reasons),
                review_attempts=ticket.review_attempts + 1,
                description=description,
            )

        error = (
            "review rejected: " + "; ".join(verdict.reasons)
            if verdict.reasons
            else "review rejected"
        )
        return ReviewDecision(
            status=TicketStatus.FAILED,
            error=_clip(error, MAX_SUMMARY_CHARS),
            review_status=REVIEW_REJECTED,
            reasons=list(verdict.reasons),
            review_attempts=ticket.review_attempts + 1,
        )

    def _run_reviewer(
        self,
        ticket: Ticket,
        outcome: ContractOutcome,
        heartbeat: "_Heartbeat",
    ) -> tuple[ReviewVerdict | None, dict[str, Any]]:
        """Call the reviewer, converting every failure into ``None``.

        A review bug, transport problem or timeout must never fail a ticket;
        the caller turns ``None`` into ``review_status=inconclusive``.
        """

        assert self.reviewer is not None
        try:
            return self.reviewer.review(
                ticket,
                outcome,
                workdir=Path(ticket.workdir),
                on_beat=heartbeat.tick,
            )
        except Exception as exc:  # noqa: BLE001 - a review bug must not fail a ticket
            self.log_event(
                "review_inconclusive",
                ticket_id=ticket.id,
                details={"reason": f"reviewer_crash: {type(exc).__name__}: {exc}"},
            )
            return None, {}

    def fail_dependents(self, ticket_id: str) -> list[Ticket]:
        """Cascade a failure to every pending ticket that depends on it.

        A dependency that failed can never be satisfied, so its dependents
        are failed too (with an explicit reason) instead of sitting pending
        forever.  Returns the tickets that were failed, transitively.
        """

        failed: list[Ticket] = []
        frontier = [ticket_id]
        while frontier:
            current = frontier.pop(0)
            for dependent in self.store.fail_dependents(
                current, reason=f"dependency failed: {current}"
            ):
                append_log(
                    dependent.log_path,
                    f"[{utc_now()}] [router] failed because dependency {current} "
                    f"failed\n",
                )
                failed.append(dependent)
                frontier.append(dependent.id)
        return failed

    def get(self, ticket_id: str) -> Ticket | None:
        """Return a ticket, or ``None`` when the id is unknown."""

        return self.store.get(ticket_id)

    def result(self, ticket_id: str) -> Ticket | None:
        """Alias for :meth:`get`, kept for the spec's ``result`` wording."""

        return self.store.get(ticket_id)

    def list(
        self,
        status: TicketStatus | str | None = None,
        limit: int | None = 50,
    ) -> list[Ticket]:
        return self.store.list_tickets(status=status, limit=limit)

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> Router:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- internals ------------------------------------------------------
    def _resolve_worker(self, worker: str | Worker | None) -> Worker:
        if isinstance(worker, Worker):
            return worker
        name = worker or self.config["default_worker"]
        adapter = self.workers.get(name)
        if adapter is None:
            known = ", ".join(sorted(self.workers))
            raise ValueError(f"unknown worker {name!r}; available workers: {known}")
        return adapter

    def _make_heartbeat(
        self, ticket: Ticket, token: str | None = None
    ) -> "_Heartbeat":
        """Return a lease-checked heartbeat ticker for ``ticket``."""

        def heartbeat() -> None:
            try:
                if self.store.heartbeat(ticket.id, claim_token=token) is not None:
                    append_log(ticket.log_path, f"[{utc_now()}] [router] heartbeat\n")
            except Exception:  # pragma: no cover - never kill a run over a tick
                pass

        return _Heartbeat(heartbeat, float(self.config["heartbeat_seconds"]))

    def _run_worker(
        self,
        adapter: Worker,
        ticket: Ticket,
        workdir: Path,
        heartbeat: "_Heartbeat",
    ) -> WorkerResult:
        """Call ``adapter.run``, supplying a heartbeat hook when supported."""

        def beat() -> None:
            heartbeat.tick()

        try:
            parameters = inspect.signature(adapter.run).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            parameters = {}
        if "on_heartbeat" in parameters:
            return adapter.run(ticket, workdir, on_heartbeat=beat)
        return adapter.run(ticket, workdir)

    def _judge(
        self, adapter: Worker, result: WorkerResult, workdir: Path
    ) -> ContractOutcome:
        """Decide success from the contract and normalize artifact paths."""

        if adapter.result_contract and result.success:
            # The process ended cleanly; only the RESULT line decides success.
            outcome = check_contract(result.output, result.exit_code)
            if not outcome.success and not outcome.summary:
                outcome.summary = result.summary
        elif adapter.result_contract:
            # Execution-level failure (not found, timeout, non-zero exit,
            # sandbox violation): keep the adapter's precise error.
            outcome = ContractOutcome(
                success=False,
                summary=result.summary,
                error=result.error or "worker failed",
            )
        else:
            # No RESULT line to parse: the adapter's own verdict is
            # authoritative, artifacts included (e.g. custom workers).
            outcome = ContractOutcome(
                success=bool(result.success),
                summary=result.summary,
                artifacts=list(result.artifacts),
                error=None if result.success else (result.error or "worker reported failure"),
            )

        artifacts: list[str] = []
        for raw in outcome.artifacts:
            normalized = normalize_artifact(raw, workdir)
            if normalized and normalized not in artifacts:
                artifacts.append(normalized)
        outcome.artifacts = artifacts
        return outcome

    def _fail(
        self, ticket_id: str, error: str, started: float, *, token: str | None = None
    ) -> None:
        finished_at = utc_now()
        try:
            ticket = self.store.set_status(
                ticket_id,
                TicketStatus.FAILED,
                expected_claim_token=token,
                error=_clip(error, MAX_SUMMARY_CHARS),
                finished_at=finished_at,
            )
            if ticket is None:
                return
            append_log(
                ticket.log_path,
                f"[{finished_at}] failed after {time.monotonic() - started:.2f}s: {error}\n",
            )
            self.fail_dependents(ticket_id)
        except Exception:  # pragma: no cover - preserving the original failure wins
            pass


@dataclass(slots=True)
class ReviewDecision:
    """What the completion path must persist after contract + review.

    ``status`` is the ticket's final status (``done``, ``failed``, or
    ``pending`` for a review-driven retry).  ``description`` is only set when
    the retry carries reviewer feedback.  ``review_attempts`` is the new
    semantic-attempt counter (kept apart from execution ``attempts``).
    """

    status: TicketStatus
    error: str | None = None
    review_status: str = REVIEW_NONE
    reasons: list[str] = field(default_factory=list)
    #: ``None`` leaves the stored counter untouched (review did not run).
    review_attempts: int | None = None
    description: str | None = None


class _Heartbeat:
    """Background ticker that refreshes a running ticket's heartbeat.

    The router owns this so every worker benefits, including ones that do not
    accept the optional ``on_heartbeat`` callback.
    """

    def __init__(self, callback: Callable[[], None], interval: float) -> None:
        self.callback = callback
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="ticket-heartbeat", daemon=True
        )
        self._thread.start()

    def tick(self) -> None:
        with self._lock:
            self.callback()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # pragma: no cover - a tick must never kill a run
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated, see log)"
