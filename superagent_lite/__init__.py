"""superagent-lite: a local router/worker agent skeleton (MVP)."""

from .config import default_config, load_config, resolve_claude_binary
from .models import Ticket, TicketStatus
from .redact import sanitize_text
from .result import (
    ContractOutcome,
    ResultContract,
    build_prompt,
    check_contract,
    parse_result_line,
)
from .router import Router
from .store import TicketStore
from .workers import ClaudeCodeWorker, CodexWorker, EchoWorker, Worker, WorkerResult

__version__ = "0.2.0"

__all__ = [
    "ClaudeCodeWorker",
    "CodexWorker",
    "ContractOutcome",
    "EchoWorker",
    "ResultContract",
    "Router",
    "Ticket",
    "TicketStatus",
    "TicketStore",
    "Worker",
    "WorkerResult",
    "build_prompt",
    "check_contract",
    "default_config",
    "load_config",
    "parse_result_line",
    "resolve_claude_binary",
    "sanitize_text",
    "__version__",
]
