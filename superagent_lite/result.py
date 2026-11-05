"""The structured result contract shared by workers and the router.

A Claude worker is asked to finish its reply with one line::

    RESULT {"status": "success", "summary": "...", "artifacts": ["file"]}

The router parses the last such line and decides the ticket status.  A reply
without a parseable ``RESULT`` line is a failure, even when the child process
exited with code 0 -- that is the "polite refusal marked done" bug this
contract exists to prevent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

RESULT_PREFIX = "RESULT"
VALID_STATUSES = ("success", "failed")

# Instructions appended to every task sent through the Claude Code adapter.
CONTRACT_INSTRUCTIONS = """
---
Superagent result contract (mandatory):
Work only inside the working directory: {workdir}
When you are completely finished, the very last line of your reply MUST be a
single line of JSON in exactly this form:
RESULT {{"status": "success", "summary": "<one-line summary>", "artifacts": ["<path relative to the working directory>"]}}
If you could not complete the task, use status "failed" and explain why in the
summary:
RESULT {{"status": "failed", "summary": "<what went wrong>", "artifacts": []}}
Do not print anything after that line. Do not wrap it in a code fence. Listing
an artifact you did not actually create is a violation of the contract.
""".strip()


@dataclass(slots=True)
class ResultContract:
    """The payload of a validated ``RESULT`` line."""

    status: str
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContractOutcome:
    """Verdict produced by :func:`check_contract`."""

    success: bool
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None


def build_prompt(task: str, workdir: str | os.PathLike[str], *, context: str = "") -> str:
    """Append the mandatory result-contract instructions to ``task``."""

    instructions = CONTRACT_INSTRUCTIONS.format(workdir=str(workdir))
    background = (
        "\n\nTask context and previous reports:\n" + context
        + "\nKeep the original request and its constraints in view. Treat previous "
        "reports as leads to verify, not proof. Make technical decisions from the "
        "actual workspace. State checks performed and unresolved issues in your "
        "result summary; write a handoff artifact if the details need more space. "
        "If a necessary user decision is missing, report failed with one concrete "
        "question in the summary for the main agent to relay. Decide ordinary "
        "implementation details yourself."
    ) if context else ""
    return f"{str(task).rstrip()}{background}\n\n{instructions}\n"


def _strip_fence(line: str) -> str:
    line = line.strip()
    if line.startswith("```"):
        line = line[3:].strip()
    if line.endswith("```"):
        line = line[:-3].strip()
    return line


def find_result_payload(text: str) -> str | None:
    """Return the JSON payload of the last ``RESULT`` line, if any.

    Returns an empty string when a ``RESULT`` line exists but has no payload,
    and ``None`` when the marker is absent entirely, so callers can tell the
    two failure modes apart.
    """

    for raw_line in reversed(str(text or "").splitlines()):
        line = _strip_fence(raw_line)
        if line.upper().startswith(RESULT_PREFIX):
            payload = line[len(RESULT_PREFIX):].lstrip()
            if payload.startswith(":"):
                payload = payload[1:].lstrip()
            return payload
    return None


def parse_result_line(text: str) -> tuple[ResultContract | None, str | None]:
    """Parse the last ``RESULT`` line.

    Returns ``(contract, None)`` on success and ``(None, reason)`` otherwise.
    """

    payload = find_result_payload(text)
    if payload is None:
        return None, "missing RESULT line in worker output"
    if not payload:
        return None, "RESULT line has no JSON payload"
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, f"invalid RESULT JSON: {exc}"
    if not isinstance(raw, dict):
        return None, "RESULT payload must be a JSON object"

    status = raw.get("status")
    if status not in VALID_STATUSES:
        return None, (
            "RESULT status must be 'success' or 'failed', "
            f"got {status!r}"
        )

    summary = raw.get("summary", "")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        return None, "RESULT summary must be a string"

    artifacts = raw.get("artifacts", [])
    if artifacts is None:
        artifacts = []
    if not isinstance(artifacts, list) or not all(
        isinstance(item, str) for item in artifacts
    ):
        return None, "RESULT artifacts must be a list of strings"

    return (
        ResultContract(
            status=status,
            summary=summary.strip(),
            artifacts=[item.strip() for item in artifacts if item.strip()],
        ),
        None,
    )


def check_contract(output: str, exit_code: int | None) -> ContractOutcome:
    """Decide a contract-required worker run from its output and exit code."""

    if exit_code not in (0, None):
        return ContractOutcome(
            success=False,
            summary=_tail(output),
            error=f"claude exited with code {exit_code}",
        )

    contract, parse_error = parse_result_line(output)
    if contract is None:
        return ContractOutcome(
            success=False,
            summary=_tail(output),
            error=parse_error or "invalid RESULT line",
        )

    if contract.status == "failed":
        return ContractOutcome(
            success=False,
            summary=contract.summary or _tail(output),
            error=contract.summary or "worker reported failure",
            artifacts=contract.artifacts,
        )

    return ContractOutcome(
        success=True,
        summary=contract.summary,
        artifacts=contract.artifacts,
    )


def normalize_artifact(path: str, workdir: str | os.PathLike[str]) -> str:
    """Normalize an artifact path to a workdir-relative POSIX-style path."""

    path = path.strip().replace("\\", "/")
    if not path:
        return path
    while path.startswith("./"):
        path = path[2:]
    if os.path.isabs(path):
        try:
            relative = os.path.relpath(path, str(workdir))
        except ValueError:  # pragma: no cover - different drive on Windows
            return path
        if not relative.startswith(".."):
            return relative.replace(os.sep, "/")
    return path


def _tail(text: str, limit: int = 1000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]
