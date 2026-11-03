"""The main agent's planning brain.

One user sentence goes in; the brain calls a planning model over an
Anthropic-compatible ``/v1/messages`` endpoint (see :mod:`.planner`) and
decides between two outcomes:

* **direct reply** -- small talk or a question the model can answer itself;
* **plan** -- 1..N tickets (with optional ``depends_on``) enqueued into the
  existing SQLite queue, executed by ``superagentd``, then summarized back
  into one reply from the workers' *structured* results.

The same brain serves the CLI REPL and the HTTP API, so both entry points
behave identically.  Nothing here stores credentials: the endpoint is
resolved at call time by :func:`superagent_lite.planner.load_endpoint`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .config import default_config, normalize_config
from .memory import read_memory
from .models import Ticket, TicketStatus, new_ticket_id, utc_now
from .planner import Endpoint, MessagesClient, PlannerError, load_endpoint
from .router import Router

MODE_REPLY = "reply"
MODE_SUMMARY = "summary"
MODE_PENDING = "pending"
MODE_ERROR = "error"

#: Workers run every ticket; these are the adapter names the planner may pick.
DEFAULT_MAX_TICKETS = 8
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CompletionFn(Protocol):
    """The slice of :class:`MessagesClient` the brain depends on."""

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Planning data structures
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PlanStep:
    """One ticket as the planner described it, before it exists in the store."""

    id: str
    description: str
    worker: str | None = None
    depends_on: list[str] = field(default_factory=list)
    workdir: str | None = None


@dataclass(slots=True)
class Plan:
    """A parsed planner decision: either a direct reply or tickets."""

    action: str
    reply: str | None = None
    steps: list[PlanStep] = field(default_factory=list)
    workdir: str | None = None
    model: str = ""
    raw: str = ""

    @property
    def is_reply(self) -> bool:
        return self.action == "reply"


@dataclass(slots=True)
class ChatReply:
    """What one :meth:`Brain.reply` call returns to any entry point."""

    text: str
    mode: str
    ticket_ids: list[str] = field(default_factory=list)
    timed_out: bool = False
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.text,
            "mode": self.mode,
            "ticket_ids": list(self.ticket_ids),
            "timed_out": self.timed_out,
            "model": self.model,
        }


class PlanParseError(PlannerError):
    """The planner answered with something that is not a valid plan."""


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------
class Brain:
    """Split a user request into tickets, then summarize their results."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        router: Router | None = None,
        client: CompletionFn | None = None,
        endpoint: Endpoint | None = None,
        memory_path: str | Path | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = normalize_config(dict(config)) if config is not None else default_config()
        self._owns_router = router is None
        self.router = router or Router(config=self.config, recover_mode="none")
        self._client = client
        self._endpoint = endpoint
        self.memory_path = Path(
            memory_path if memory_path is not None else self.config["memory_path"]
        ).expanduser()
        self._sleep = sleep

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        if self._owns_router:
            self.router.close()

    def __enter__(self) -> Brain:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- public API -----------------------------------------------------
    def reply(
        self,
        text: str,
        *,
        user_id: str = "local",
        timeout: float | None = None,
    ) -> ChatReply:
        """Handle one user message end to end.

        Never raises for planner/endpoint problems: those come back as a
        ``MODE_ERROR`` reply so a REPL or HTTP client always gets an answer.
        """

        text = (text or "").strip()
        if not text:
            return ChatReply(text="(empty message)", mode=MODE_ERROR)

        try:
            plan = self.plan(text, user_id=user_id)
        except PlannerError as exc:
            return ChatReply(text=f"planner error: {exc}", mode=MODE_ERROR)
        except Exception as exc:  # noqa: BLE001 - never kill the chat loop
            return ChatReply(
                text=f"internal error while planning: {type(exc).__name__}: {exc}",
                mode=MODE_ERROR,
            )

        if plan.is_reply:
            return ChatReply(
                text=plan.reply or "", mode=MODE_REPLY, model=plan.model
            )

        try:
            tickets = self.enqueue(plan)
        except Exception as exc:  # noqa: BLE001 - surface as a reply, not a crash
            return ChatReply(
                text=f"could not queue the plan: {type(exc).__name__}: {exc}",
                mode=MODE_ERROR,
            )

        wait = float(self.config["chat_timeout_seconds"] if timeout is None else timeout)
        finished, timed_out = self.wait(tickets, timeout=wait)
        ids = [ticket.id for ticket in tickets]

        if timed_out:
            pending = [t.id for t in finished if not t.is_terminal]
            state = ", ".join(
                f"{t.id}={t.status.value}" for t in finished
            )
            return ChatReply(
                text=(
                    "Working on it. Tickets: " + ", ".join(ids) + "\n"
                    f"status: {state}\n"
                    + (f"still pending: {', '.join(pending)}\n" if pending else "")
                    + "Ask again later or use `show <ticket_id>` for details."
                ),
                mode=MODE_PENDING,
                ticket_ids=ids,
                timed_out=True,
            )

        try:
            summary = self.summarize(text, finished)
        except PlannerError as exc:
            summary = (
                "All tickets finished, but summarizing the results failed: "
                f"{exc}\n\n"
                + "\n".join(_describe_ticket(t) for t in finished)
            )
        return ChatReply(
            text=summary,
            mode=MODE_SUMMARY,
            ticket_ids=ids,
            model=None,
        )

    def plan(self, text: str, *, user_id: str = "local") -> Plan:
        """Ask the planner model to decide what to do with ``text``."""

        system = self._planner_system_prompt()
        messages = [
            {
                "role": "user",
                "content": f"User ({user_id}) says:\n{text}",
            }
        ]
        model = str(self.config["brain_model"])
        raw = self._complete(model=model, system=system, messages=messages)
        plan = parse_plan(raw, max_tickets=int(self.config["brain_max_tickets"]), model=model)

        advanced = self.config.get("brain_advanced_model")
        if (
            plan.action == "plan"
            and advanced
            and self.config.get("brain_escalate_on_complex")
            and _looks_complex(raw)
        ):
            raw = self._complete(
                model=str(advanced), system=system, messages=messages
            )
            plan = parse_plan(
                raw, max_tickets=int(self.config["brain_max_tickets"]), model=str(advanced)
            )
        return plan

    def enqueue(self, plan: Plan) -> list[Ticket]:
        """Create every ticket of ``plan`` in dependency order."""

        if not plan.steps:
            raise PlanParseError("plan has no tickets")

        steps = _topological_order(plan.steps)
        default_worker = str(self.config.get("brain_worker") or "claude")
        workers = set(self.router.workers)

        # One shared directory per request keeps multi-ticket work connected
        # (step 2 can see what step 1 wrote) while still isolating it from
        # other requests.  An explicit path from the planner wins.
        shared_workdir = Path(self.router.work_dir) / (
            f"brain_{utc_now().replace(':', '').replace('-', '')}_{new_ticket_id()[-6:]}"
        )
        plan_workdir = _absolute_or_none(plan.workdir)

        step_to_ticket: dict[str, str] = {}
        tickets: list[Ticket] = []
        for step in steps:
            worker = step.worker if step.worker in workers else None
            workdir = (
                _absolute_or_none(step.workdir)
                or plan_workdir
                or shared_workdir
            )
            dependencies = [
                step_to_ticket[dependency]
                for dependency in step.depends_on
                if dependency in step_to_ticket
            ]
            ticket = self.router.enqueue(
                step.description,
                worker=worker or default_worker,
                depends_on=dependencies,
                workdir=workdir,
            )
            step_to_ticket[step.id] = ticket.id
            tickets.append(ticket)
        return tickets

    def wait(self, tickets: Sequence[Ticket], *, timeout: float) -> tuple[list[Ticket], bool]:
        """Poll until every ticket is terminal or ``timeout`` expires."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        interval = max(0.01, float(self.config["chat_poll_seconds"]))
        while True:
            current = [
                self.router.get(ticket.id) or ticket for ticket in tickets
            ]
            if all(ticket.is_terminal for ticket in current):
                return current, False
            if time.monotonic() >= deadline:
                return current, True
            self._sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def summarize(self, request: str, tickets: Sequence[Ticket]) -> str:
        """Turn the structured ticket results into one user-facing reply."""

        payload = json.dumps(
            {
                "request": request,
                "tickets": [_result_dict(ticket) for ticket in tickets],
            },
            ensure_ascii=False,
            indent=2,
        )
        system = (
            "You are the main agent of a self-hosted assistant. Execution "
            "agents have finished the tickets below. Write the final answer "
            "to the user: what was actually done, which artifacts were "
            "created, and any failure honestly. Do not invent results, "
            "files or success that the ticket data does not show. Be "
            "concise and answer in the language of the request."
        )
        return self._complete(
            model=str(self.config["brain_model"]),
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Ticket results as JSON:\n{payload}",
                }
            ],
            max_tokens=int(self.config["brain_max_tokens"]),
        )

    # -- internals ------------------------------------------------------
    def _complete(self, **kwargs: Any) -> str:
        client = self._get_client()
        kwargs.setdefault("max_tokens", int(self.config["brain_max_tokens"]))
        return client.complete(**kwargs)

    def _get_client(self) -> CompletionFn:
        if self._client is not None:
            return self._client
        if self._endpoint is None:
            self._endpoint = load_endpoint(self.config["brain_settings_path"])
        self._client = MessagesClient(
            self._endpoint,
            timeout=float(self.config["brain_timeout_seconds"]),
        )
        return self._client

    def _planner_system_prompt(self) -> str:
        workers = ", ".join(sorted(self.router.workers))
        prompt = PLANNER_SYSTEM.format(
            workers=workers,
            default_worker=self.config.get("brain_worker") or "claude",
            max_tickets=int(self.config["brain_max_tickets"]),
        )
        memory = read_memory(
            self.memory_path, int(self.config["brain_memory_max_chars"])
        )
        if memory:
            prompt += (
                "\n\nLong-term memory about this user and their projects "
                "(maintained by hand; use it to interpret requests):\n"
                f"{memory}\n"
            )
        return prompt


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
PLANNER_SYSTEM = """\
You are the main agent (planner) of superagent-lite, a self-hosted assistant.
You are the only agent the user talks to. Execution agents ("workers") run
tasks through a queue and see only the ticket description and their working
directory; you never touch the user's machine yourself.

Decide one of two actions for the user's message:

1. "reply" -- small talk, questions you can answer from your own knowledge, or
   anything that does not need the user's machine. Answer directly and briefly.
2. "plan" -- anything that must read, create or modify files, run commands, or
   otherwise inspect or change the user's system. Split it into 1..{max_tickets}
   tickets.

Reply with exactly one JSON object and nothing else (no prose, no code fence):

{{"action": "reply", "reply": "<your answer>"}}

or

{{"action": "plan", "workdir": null,
  "tickets": [
    {{"id": "t1", "description": "<complete instruction>", "worker": "{default_worker}", "depends_on": []}},
    {{"id": "t2", "description": "<complete instruction>", "worker": "{default_worker}", "depends_on": ["t1"]}}
  ]}}

Rules:
- A ticket description must stand alone: the worker sees only that text and
  its working directory. Name exact files, intended contents, and paths.
- "depends_on" lists ticket ids that must finish first. Add it whenever a
  step needs an earlier step's output. Independent steps run in parallel.
- "workdir" is the absolute directory the user asked to work in, or null.
  When null, all tickets of this request share one private working directory.
- Available workers: {workers}. Use "{default_worker}" for anything that needs
  file or shell access. Ticket ids are t1, t2, ... and must be unique.
- Never say work is done; only workers execute. Do not describe results you
  have not received yet.
- Answer in the language the user wrote in.
"""


# ---------------------------------------------------------------------------
# Parsing / helpers
# ---------------------------------------------------------------------------
def parse_plan(raw: str, *, max_tickets: int = DEFAULT_MAX_TICKETS, model: str = "") -> Plan:
    """Parse and validate one planner answer.

    A non-JSON answer is accepted as a direct reply (models occasionally drop
    the envelope); a JSON object that fails validation is a hard error, so a
    malformed plan never reaches the queue.
    """

    text = (raw or "").strip()
    if not text:
        raise PlanParseError("planner returned an empty answer")

    candidate = _json_candidate(text)
    if candidate is None:
        if _looks_like_json(text):
            raise PlanParseError("planner started a JSON object but did not finish it")
        return Plan(action="reply", reply=text, model=model, raw=raw)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if _looks_like_json(text):
            raise PlanParseError(f"planner JSON is invalid: {exc}") from None
        return Plan(action="reply", reply=text, model=model, raw=raw)

    if not isinstance(data, dict):
        if _looks_like_json(text):
            raise PlanParseError("planner JSON must be an object")
        return Plan(action="reply", reply=text, model=model, raw=raw)

    action = str(data.get("action") or "").strip().lower()
    if not action:
        if "tickets" in data:
            action = "plan"
        elif "reply" in data:
            action = "reply"

    if action == "reply":
        reply = data.get("reply")
        if reply is None:
            reply = data.get("text")
        if not isinstance(reply, str) or not reply.strip():
            raise PlanParseError("planner reply is missing or empty")
        return Plan(action="reply", reply=reply.strip(), model=model, raw=raw)

    if action != "plan":
        raise PlanParseError(f"unknown planner action: {action!r}")

    steps = _parse_steps(data.get("tickets"), max_tickets=max_tickets)
    workdir = data.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        workdir = None
    return Plan(
        action="plan",
        steps=steps,
        workdir=workdir or None,
        model=model,
        raw=raw,
    )


def _parse_steps(raw: Any, *, max_tickets: int) -> list[PlanStep]:
    if not isinstance(raw, list) or not raw:
        raise PlanParseError("planner plan must contain a non-empty ticket list")
    if len(raw) > max_tickets:
        raise PlanParseError(
            f"planner returned {len(raw)} tickets; the limit is {max_tickets}"
        )

    steps: list[PlanStep] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PlanParseError(f"ticket {index} must be a JSON object")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PlanParseError(f"ticket {index} has no description")
        step_id = item.get("id")
        if not isinstance(step_id, str) or not _STEP_ID_RE.match(step_id.strip()):
            step_id = f"t{index}"
        step_id = step_id.strip()
        if step_id in seen:
            raise PlanParseError(f"duplicate ticket id: {step_id}")
        seen.add(step_id)

        worker = item.get("worker")
        if not isinstance(worker, str) or not worker.strip():
            worker = None
        depends_on = item.get("depends_on") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if not isinstance(depends_on, list) or not all(
            isinstance(dep, str) for dep in depends_on
        ):
            raise PlanParseError(f"ticket {step_id}: depends_on must be a list of ids")
        workdir = item.get("workdir")
        if not isinstance(workdir, str) or not workdir.strip():
            workdir = None

        steps.append(
            PlanStep(
                id=step_id,
                description=description.strip(),
                worker=worker.strip() if worker else None,
                depends_on=[dep.strip() for dep in depends_on if dep.strip()],
                workdir=workdir,
            )
        )

    known = {step.id for step in steps}
    for step in steps:
        unknown = [dep for dep in step.depends_on if dep not in known]
        if unknown:
            raise PlanParseError(
                f"ticket {step.id} depends on unknown ticket(s): {unknown}"
            )
    _topological_order(steps)  # raises on cycles
    return steps


def _topological_order(steps: Sequence[PlanStep]) -> list[PlanStep]:
    """Order steps so dependencies come first; raise on an unknown/cycle."""

    by_id = {step.id: step for step in steps}
    resolved: list[PlanStep] = []
    placed: set[str] = set()
    pending = list(steps)

    while pending:
        progressed = False
        for step in list(pending):
            if all(dependency in placed for dependency in step.depends_on):
                resolved.append(step)
                placed.add(step.id)
                pending.remove(step)
                progressed = True
        if not progressed:
            cycle = ", ".join(step.id for step in pending)
            raise PlanParseError(f"dependency cycle or missing ticket among: {cycle}")

    # Reachability is guaranteed by the loop; keep the check explicit.
    assert all(step.id in by_id for step in resolved)
    return resolved


def _json_candidate(text: str) -> str | None:
    """Return the JSON object embedded in ``text``, if there is one."""

    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop an opening fence line and any closing fence.
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return stripped[start : end + 1]


def _looks_like_json(text: str) -> bool:
    """True when the answer *intends* to be a JSON object (so malformed = error)."""

    return text.strip().startswith("{")


def _looks_complex(raw: str) -> bool:
    candidate = _json_candidate(raw)
    if candidate is None:
        return False
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and bool(data.get("complex"))


def _absolute_or_none(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else None


def _result_dict(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "description": ticket.description,
        "status": ticket.status.value,
        "worker": ticket.worker,
        "workdir": ticket.workdir,
        "summary": ticket.result_summary,
        "artifacts": list(ticket.artifacts),
        "error": ticket.error,
        "exit_code": ticket.exit_code,
    }


def _describe_ticket(ticket: Ticket) -> str:
    line = f"- {ticket.id} [{ticket.status.value}] {ticket.result_summary or ticket.error or ''}"
    if ticket.artifacts:
        line += f" (artifacts: {', '.join(ticket.artifacts)})"
    return line
