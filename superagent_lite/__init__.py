"""superagent-lite: a local router/worker agent skeleton (MVP)."""

from .config import default_config, load_config
from .models import Ticket, TicketStatus
from .router import Router
from .store import TicketStore
from .workers import ClaudeCodeWorker, CodexWorker, EchoWorker, Worker, WorkerResult

__version__ = "0.1.0"

__all__ = [
    "ClaudeCodeWorker",
    "CodexWorker",
    "EchoWorker",
    "Router",
    "Ticket",
    "TicketStatus",
    "TicketStore",
    "Worker",
    "WorkerResult",
    "default_config",
    "load_config",
    "__version__",
]
