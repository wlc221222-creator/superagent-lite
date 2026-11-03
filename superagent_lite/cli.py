"""Command-line interface: ``python -m superagent_lite``."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import load_config
from .daemon import Daemon, DaemonAlreadyRunning
from .models import Ticket, TicketStatus
from .router import Router

STATUS_CHOICES = [status.value for status in TicketStatus]
COMMANDS = ("run", "queue", "daemon", "list", "show")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="path to config.json (default: ./config.json, else built-in defaults)",
    )

    parser = argparse.ArgumentParser(
        prog="superagent",
        description="superagent-lite: a local router/worker agent skeleton.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run", parents=[common], help="create a ticket, execute it, print the result"
    )
    run.add_argument("task", nargs="+", help="task description (quote it)")
    run.add_argument(
        "--worker",
        default=None,
        help="worker adapter: echo | claude | codex (default: config.default_worker)",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="timeout in seconds (default: config.timeout_seconds)",
    )

    queue = subparsers.add_parser(
        "queue",
        parents=[common],
        help="create a pending ticket without executing it (superagentd runs it)",
    )
    queue.add_argument("task", nargs="+", help="task description (quote it)")
    queue.add_argument(
        "--worker",
        default=None,
        help="worker adapter: echo | claude | codex (default: config.default_worker)",
    )
    queue.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="timeout in seconds (default: config.timeout_seconds)",
    )
    queue.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )

    daemon_cmd = subparsers.add_parser(
        "daemon",
        parents=[common],
        help="run superagentd in the foreground (claims and executes queued tickets)",
    )
    daemon_cmd.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="worker pool size (default: config.daemon_concurrency)",
    )
    daemon_cmd.add_argument(
        "--poll",
        type=float,
        default=None,
        help="queue poll interval in seconds (default: config.daemon_poll_seconds)",
    )
    daemon_cmd.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="requeues allowed per interrupted ticket (default: config.max_retries)",
    )

    list_cmd = subparsers.add_parser(
        "list", parents=[common], help="list tickets, newest first"
    )
    list_cmd.add_argument(
        "--status", choices=STATUS_CHOICES, default=None, help="filter by status"
    )
    list_cmd.add_argument(
        "--limit", type=int, default=50, help="maximum rows to show (default: 50)"
    )
    list_cmd.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )

    show = subparsers.add_parser(
        "show", parents=[common], help="show one ticket's details and result"
    )
    show.add_argument("ticket_id", help="ticket id (see `list`)")
    show.add_argument("--log", action="store_true", help="also print the full log")
    show.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )

    return parser


def _hoist_global_config(argv: list[str]) -> list[str]:
    """Let ``--config PATH`` appear before or after the sub-command.

    ``argparse`` only accepts it on the common sub-parser, which forces
    ``show --config X <id>``; users reasonably type ``--config X show <id>``.
    Only a bare ``--config`` directly followed by a value is hoisted.
    """

    hoisted: list[str] = []
    rest: list[str] = []
    commands = set(COMMANDS)
    index = 0
    while index < len(argv):
        if argv[index] == "--config" and index + 1 < len(argv):
            hoisted.extend(["--config", argv[index + 1]])
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    if not rest or rest[0] not in commands:
        return hoisted + rest
    # ``--config`` belongs *after* the sub-command for argparse.
    return [rest[0]] + hoisted + rest[1:]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    # Supervise a foreground daemon from a pipe (``daemon > out.log``) means
    # block-buffered stdout; line buffering keeps the operator informed live.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    args = parser.parse_args(_hoist_global_config(raw))

    try:
        config = load_config(args.config, require=args.config is not None)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command in ("run", "queue") and args.timeout is not None:
        config["timeout_seconds"] = args.timeout

    if args.command == "daemon":
        if args.concurrency is not None and args.concurrency < 1:
            print("error: --concurrency must be at least 1", file=sys.stderr)
            return 2
        if args.poll is not None and args.poll <= 0:
            print("error: --poll must be greater than 0", file=sys.stderr)
            return 2
        if args.max_retries is not None and args.max_retries < 0:
            print("error: --max-retries must be >= 0", file=sys.stderr)
            return 2
        daemon = Daemon(
            config=config,
            concurrency=args.concurrency,
            poll_seconds=args.poll,
            max_retries=args.max_retries,
        )
        try:
            return daemon.run()
        except DaemonAlreadyRunning as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        with Router(config=config) as router:
            if args.command == "run":
                return _cmd_run(router, args)
            if args.command == "queue":
                return _cmd_queue(router, args)
            if args.command == "list":
                return _cmd_list(router, args)
            if args.command == "show":
                return _cmd_show(router, args)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


def _cmd_run(router: Router, args: argparse.Namespace) -> int:
    description = " ".join(args.task)
    try:
        ticket = router.submit(description, worker=args.worker)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"ticket:  {ticket.id}")
    print(f"status:  {ticket.status.value}")
    print(f"worker:  {ticket.worker}")
    print(f"workdir: {ticket.workdir}")
    print(f"log:     {ticket.log_path}")
    if ticket.result_summary:
        print("result:")
        print(ticket.result_summary)
    if ticket.artifacts:
        print("artifacts:")
        for artifact in ticket.artifacts:
            print(f"  - {artifact}")
    if ticket.error:
        print(f"error:   {ticket.error}")
    return 0 if ticket.status is TicketStatus.DONE else 1


def _cmd_queue(router: Router, args: argparse.Namespace) -> int:
    description = " ".join(args.task)
    try:
        ticket = router.enqueue(description, worker=args.worker)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_ticket_dict(ticket), ensure_ascii=False, indent=2))
        return 0
    print(f"ticket:  {ticket.id}")
    print(f"status:  {ticket.status.value}")
    print(f"worker:  {ticket.worker}")
    print(f"workdir: {ticket.workdir}")
    print(f"log:     {ticket.log_path}")
    print("queued:  waiting for superagentd (`python -m superagent_lite daemon`)")
    return 0


def _cmd_list(router: Router, args: argparse.Namespace) -> int:
    if args.limit <= 0:
        print("error: --limit must be greater than 0", file=sys.stderr)
        return 2
    tickets = router.list(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps([_ticket_dict(t) for t in tickets], ensure_ascii=False, indent=2))
        return 0
    if not tickets:
        print("No tickets.")
        return 0

    print(f"{'ID':<26} {'STATUS':<9} {'WORKER':<8} {'CREATED_AT':<26} DESCRIPTION")
    for ticket in tickets:
        description = " ".join(ticket.description.split())
        if len(description) > 60:
            description = description[:57] + "..."
        print(
            f"{ticket.id:<26} {ticket.status.value:<9} {ticket.worker:<8} "
            f"{ticket.created_at:<26} {description}"
        )
    return 0


def _cmd_show(router: Router, args: argparse.Namespace) -> int:
    ticket = router.get(args.ticket_id)
    if ticket is None:
        print(f"error: ticket not found: {args.ticket_id}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_ticket_dict(ticket), ensure_ascii=False, indent=2))
    else:
        _print_ticket(ticket)

    if args.log:
        print("\n--- log ---")
        if ticket.log_path and Path(ticket.log_path).exists():
            print(Path(ticket.log_path).read_text(encoding="utf-8"), end="")
        else:
            print("(no log file)")
    return 0


def _print_ticket(ticket: Ticket) -> None:
    print(f"id:           {ticket.id}")
    print(f"description:  {ticket.description}")
    print(f"status:       {ticket.status.value}")
    print(f"worker:       {ticket.worker}")
    print(f"workdir:      {ticket.workdir}")
    print(f"timeout:      {ticket.timeout:g}s")
    print(f"created_at:   {ticket.created_at}")
    print(f"started_at:   {ticket.started_at or '-'}")
    print(f"heartbeat_at: {ticket.heartbeat_at or '-'}")
    print(f"finished_at:  {ticket.finished_at or '-'}")
    print(f"exit_code:    {ticket.exit_code if ticket.exit_code is not None else '-'}")
    print(f"attempts:     {ticket.attempts}")
    print(f"retry:        {ticket.retry_count}/{ticket.max_retries}")
    print(f"error:        {ticket.error or '-'}")
    print(f"log:          {ticket.log_path or '-'}")
    print("result:")
    print(ticket.result_summary or "(no result yet)")
    print("artifacts:")
    if ticket.artifacts:
        for artifact in ticket.artifacts:
            print(f"  - {artifact}")
    else:
        print("  (none)")


def _ticket_dict(ticket: Ticket) -> dict[str, object]:
    return {
        "id": ticket.id,
        "description": ticket.description,
        "status": ticket.status.value,
        "worker": ticket.worker,
        "workdir": ticket.workdir,
        "timeout": ticket.timeout,
        "result_summary": ticket.result_summary,
        "artifacts": list(ticket.artifacts),
        "log_path": ticket.log_path,
        "error": ticket.error,
        "exit_code": ticket.exit_code,
        "attempts": ticket.attempts,
        "retry_count": ticket.retry_count,
        "max_retries": ticket.max_retries,
        "claim_token": ticket.claim_token,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "started_at": ticket.started_at,
        "heartbeat_at": ticket.heartbeat_at,
        "finished_at": ticket.finished_at,
    }
