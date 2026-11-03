"""Append-only decision log: ``<runtime_dir>/decisions.jsonl``.

Every record is one JSON object on one line::

    {"ts": "...", "event": "...", "ticket_id": null,
     "details": {...}, "prev_hash": "<64 hex>", "hash": "<64 hex>"}

``hash`` is ``sha256(prev_hash + canonical_json(record_without_hash))``; the
genesis record's ``prev_hash`` is 64 zeros.  The chain makes silent edits to
earlier lines detectable (see :func:`verify_chain`), which is all phase 5
promises -- Revision 4 downgraded this to a plain prev-hash link: no HMAC, no
key management, no daily anchors.

Concurrency and crash safety (Revision 2 §8):

* every append takes an inter-process ``fcntl.flock`` on the log file, so the
  daemon, a CLI ``approve`` and an HTTP request cannot interleave a line;
* each append recomputes ``prev_hash`` from the file tail *inside* the lock, so
  a write that failed midway cannot corrupt the chain, and a truncated last
  line left by a crash is dropped before writing.

Write failures never block business: :meth:`DecisionLog.append` swallows the
error, counts it and prints one line to stderr (journal-visible), then returns
``None``.  :func:`open_decision_log` returns a disabled no-op logger when the
log is switched off in config.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # POSIX only; the log lives next to the daemon.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

DECISIONS_FILENAME = "decisions.jsonl"
GENESIS_PREV_HASH = "0" * 64
MAX_DETAIL_CHARS = 2000
_SCAN_CHUNK = 64 * 1024


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, compact separators, UTF-8 preserved."""

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def record_hash(prev_hash: str, record_without_hash: Mapping[str, Any]) -> str:
    """Chain hash of one record, given the previous record's hash."""

    material = prev_hash + canonical_json(record_without_hash)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def truncate_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy ``details`` with string values capped at ``MAX_DETAIL_CHARS``.

    Lists are capped too (strings inside them are shortened): a reviewer that
    answers with a huge ``reasons`` array must not make the log line grow
    without bound.
    """

    result: dict[str, Any] = {}

    def trimmed(value: Any) -> Any:
        if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
            return value[:MAX_DETAIL_CHARS] + "...(truncated)"
        if isinstance(value, list):
            return [trimmed(item) for item in value[:50]]
        return value

    for key, value in (details or {}).items():
        result[str(key)] = trimmed(value)
    return result


class DecisionLog:
    """Append-only, flock-guarded, prev-hash-chained JSONL writer."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self.failures = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    # -- append ---------------------------------------------------------
    def append(
        self,
        event: str,
        *,
        ticket_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> str | None:
        """Append one record; return its hash, or ``None`` on failure."""

        if self.path is None:
            return None
        stamp = ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            with self._lock:
                return self._append_locked(stamp, str(event), ticket_id, details)
        except Exception as exc:  # noqa: BLE001 - logging must never break business
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                print(
                    f"[decision-log] write failed ({self.last_error}); "
                    f"event={event!r} ticket={ticket_id!r} "
                    f"failures={self.failures}",
                    file=sys.stderr,
                )
            except Exception:  # pragma: no cover - stderr itself is broken
                pass
            return None

    def _append_locked(
        self,
        stamp: str,
        event: str,
        ticket_id: str | None,
        details: Mapping[str, Any] | None,
    ) -> str:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                prev_hash, valid_size = _tail_hash(handle)
                handle.seek(0, os.SEEK_END)
                if valid_size < handle.tell():
                    # A crash left an incomplete (or unchai­nable) last line.
                    # An incomplete record was never a record: drop it.
                    handle.truncate(valid_size)
                record: dict[str, Any] = {
                    "ts": stamp,
                    "event": event,
                    "ticket_id": ticket_id,
                    "details": truncate_details(details),
                    "prev_hash": prev_hash,
                }
                digest = record_hash(prev_hash, record)
                record["hash"] = digest
                line = (canonical_json(record) + "\n").encode("utf-8")
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                return digest
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:  # pragma: no cover - close releases it
                        pass

    # -- reads ----------------------------------------------------------
    def read(self) -> list[dict[str, Any]]:
        """Return every parseable record (used by CLI ``decisions``/tests)."""

        if self.path is None or not self.path.exists():
            return []
        return read_records(self.path)

    def close(self) -> None:
        """Present for symmetry with other resources; nothing to release."""


class _NoopDecisionLog(DecisionLog):
    """A disabled log: every append is a no-op."""

    def __init__(self) -> None:  # pragma: no cover - trivial
        super().__init__(None)


def open_decision_log(
    runtime_dir: str | Path | None,
    *,
    enabled: bool | None = None,
) -> DecisionLog:
    """Open the log for ``runtime_dir`` (disabled when configured off)."""

    if enabled is False or runtime_dir is None:
        return _NoopDecisionLog()
    return DecisionLog(Path(runtime_dir).expanduser() / DECISIONS_FILENAME)


# ---------------------------------------------------------------------------
# Tail reading and chain verification
# ---------------------------------------------------------------------------
def _last_line_bounds(handle: Any, end_limit: int) -> tuple[int, int] | None:
    """Byte offsets ``(start, end)`` of the last newline-terminated line.

    Reads backwards in chunks; a trailing partial line (no newline) is not
    returned, so the caller can truncate it.
    """

    if end_limit <= 0:
        return None
    position = end_limit
    buffer = b""
    while position > 0:
        read_size = min(_SCAN_CHUNK, position)
        position -= read_size
        handle.seek(position)
        buffer = handle.read(read_size) + buffer
        newline = buffer.rfind(b"\n")
        if newline == -1:
            continue
        previous = buffer.rfind(b"\n", 0, newline)
        if previous == -1 and position > 0:
            continue  # the line starts before this buffer; read more
        start = position + previous + 1 if previous != -1 else 0
        return start, position + newline + 1
    return None


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _tail_hash(handle: Any) -> tuple[str, int]:
    """Return ``(prev_hash, valid_end_offset)`` for the current file tail.

    ``valid_end_offset`` is where the next line may be written; everything
    after it is dropped by the caller.  Walks back over lines that cannot be
    parsed rather than aborting every future append because of one bad line.
    """

    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    limit = size
    while True:
        bounds = _last_line_bounds(handle, limit)
        if bounds is None:
            return GENESIS_PREV_HASH, 0
        start, end = bounds
        handle.seek(start)
        record = _parse_line(handle.read(end - start))
        if record is not None and isinstance(record.get("hash"), str):
            return record["hash"], end
        limit = end - 1  # drop the unusable line and retry on the one before


def iter_records(path: str | Path) -> Iterator[tuple[int, dict[str, Any] | None, str]]:
    """Yield ``(line_number, record_or_None, raw_line)`` for every line."""

    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            raw = line.rstrip("\n")
            if not raw.strip():
                yield number, None, raw
                continue
            yield number, _parse_line(raw.encode("utf-8")), raw


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """Return every parseable record, skipping blank/garbled lines."""

    return [record for _number, record, _raw in iter_records(path) if record]


@dataclass(slots=True)
class ChainStatus:
    """Result of :func:`verify_chain`."""

    ok: bool
    count: int = 0
    first_bad_line: int | None = None
    error: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "count": self.count,
            "first_bad_line": self.first_bad_line,
            "error": self.error,
        }


def verify_chain(path: str | Path) -> ChainStatus:
    """Validate the whole chain, reporting the first bad line number.

    A line is bad when it is not JSON, lacks a string ``hash``/``prev_hash``,
    does not link to the previous record, or was edited (its recomputed hash
    differs).  A *partial* last line (no trailing newline, unparseable) is
    treated as a crash remnant, not tampering, and stops verification with the
    records read so far being valid.
    """

    target = Path(path)
    if not target.exists():
        return ChainStatus(ok=True, count=0)
    raw_bytes = target.read_bytes()
    lines = raw_bytes.split(b"\n")
    trailing_partial = bool(lines and lines[-1] and not raw_bytes.endswith(b"\n"))
    if lines and lines[-1] == b"":
        lines = lines[:-1]

    expected_prev = GENESIS_PREV_HASH
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, start=1):
        record = _parse_line(raw)
        if record is None:
            if trailing_partial and index == len(lines):
                return ChainStatus(
                    ok=True,
                    count=len(records),
                    records=records,
                    error="partial last line ignored",
                )
            return ChainStatus(
                ok=False,
                count=len(records),
                first_bad_line=index,
                error="line is not a JSON object",
                records=records,
            )
        if not isinstance(record.get("hash"), str) or not isinstance(
            record.get("prev_hash"), str
        ):
            return ChainStatus(
                ok=False,
                count=len(records),
                first_bad_line=index,
                error="record lacks hash/prev_hash",
                records=records,
            )
        if record["prev_hash"] != expected_prev:
            return ChainStatus(
                ok=False,
                count=len(records),
                first_bad_line=index,
                error="prev_hash does not link to the previous record",
                records=records,
            )
        body = {key: value for key, value in record.items() if key != "hash"}
        if record_hash(expected_prev, body) != record["hash"]:
            return ChainStatus(
                ok=False,
                count=len(records),
                first_bad_line=index,
                error="hash mismatch (record was modified)",
                records=records,
            )
        expected_prev = record["hash"]
        records.append(record)
    return ChainStatus(ok=True, count=len(records), records=records)
