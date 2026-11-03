"""superagent-lite: a local router/worker agent skeleton (MVP)."""

from .artifacts import ArtifactError, artifact_entries, resolve_artifact
from .brain import Brain, ChatReply, Plan, PlanStep, parse_plan
from .channels import (
    EventDeduper,
    FeishuMessage,
    FeishuSettings,
    handle_event,
    parse_event,
)
from .config import default_config, load_config, resolve_claude_binary
from .daemon import Daemon, DaemonAlreadyRunning
from .http_api import ApiConfig, make_server
from .memory import read_memory
from .models import Ticket, TicketStatus
from .planner import (
    Endpoint,
    MessagesClient,
    PlannerConfigError,
    PlannerError,
    load_endpoint,
)
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
    "ApiConfig",
    "ArtifactError",
    "Brain",
    "ChatReply",
    "ClaudeCodeWorker",
    "CodexWorker",
    "ContractOutcome",
    "Daemon",
    "DaemonAlreadyRunning",
    "EchoWorker",
    "Endpoint",
    "EventDeduper",
    "FeishuMessage",
    "FeishuSettings",
    "MessagesClient",
    "Plan",
    "PlanStep",
    "PlannerConfigError",
    "PlannerError",
    "ResultContract",
    "Router",
    "Ticket",
    "TicketStatus",
    "TicketStore",
    "Worker",
    "WorkerResult",
    "artifact_entries",
    "build_prompt",
    "check_contract",
    "default_config",
    "handle_event",
    "load_config",
    "load_endpoint",
    "make_server",
    "parse_event",
    "parse_plan",
    "parse_result_line",
    "read_memory",
    "resolve_artifact",
    "resolve_claude_binary",
    "sanitize_text",
    "__version__",
]
