"""Review corpus loading and offline evaluation (phase 5 §4).

The corpus is a directory of JSON files, one per case::

    {"description": "...", "artifacts": {"name": "content"},
     "expected": "pass"|"reject", "note": "..."}

Ground truth lives only in the file: cases are defective or clean *by
construction*, and no model output ever influences ``expected``.  The unit
tests use the corpus with a fake transport; ``tools/run_review_eval.py`` runs
it against real models (the A/B comparison of Revision 4 happens offline here,
never in production).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .models import Ticket
from .reviewer import Reviewer, ReviewVerdict

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "review_corpus"
VALID_EXPECTATIONS = ("pass", "reject")


class CorpusError(ValueError):
    """The corpus is missing cases or a case is malformed."""


@dataclass(slots=True)
class CorpusCase:
    name: str
    description: str
    artifacts: dict[str, str]
    expected: str
    note: str = ""

    def ticket(self, workdir: Path) -> Ticket:
        """Materialise the case as a reviewable ticket in ``workdir``."""

        for name, content in self.artifacts.items():
            target = workdir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return Ticket(
            id=f"corpus_{self.name}",
            description=self.description,
            worker="claude",
            origin="corpus",
            workdir=str(workdir),
            artifacts=list(self.artifacts),
        )


@dataclass(slots=True)
class EvalCaseResult:
    case: CorpusCase
    verdict: ReviewVerdict
    meta: dict

    @property
    def correct(self) -> bool:
        return self.verdict.verdict == self.case.expected


@dataclass(slots=True)
class EvalReport:
    model: str
    results: list[EvalCaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(1 for result in self.results if result.correct)

    @property
    def accuracy(self) -> float:
        return (self.correct / self.total) if self.total else 0.0

    @property
    def inconclusive(self) -> int:
        return sum(
            1 for result in self.results if result.verdict.verdict == "inconclusive"
        )

    @property
    def red_line_misses(self) -> list[EvalCaseResult]:
        """Rejects (defective cases) that were (wrongly) passed."""

        return [
            result
            for result in self.results
            if result.case.expected == "reject" and result.verdict.verdict == "pass"
        ]


def load_corpus(path: str | Path | None = None, *, minimum: int = 10) -> list[CorpusCase]:
    """Load and validate every ``*.json`` case in ``path``.

    Raises :class:`CorpusError` when fewer than ``minimum`` cases exist, a
    case is malformed, or duplicate names appear.
    """

    directory = Path(path) if path is not None else DEFAULT_CORPUS_DIR
    if not directory.is_dir():
        raise CorpusError(f"review corpus directory not found: {directory}")
    cases: list[CorpusCase] = []
    seen: set[str] = set()
    for file in sorted(directory.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorpusError(f"{file.name}: cannot read case: {exc}") from exc
        if not isinstance(data, dict):
            raise CorpusError(f"{file.name}: case must be a JSON object")
        name = file.stem
        if name in seen:
            raise CorpusError(f"duplicate case name: {name}")
        seen.add(name)
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            raise CorpusError(f"{file.name}: description must be a non-empty string")
        artifacts = data.get("artifacts", {})
        if not isinstance(artifacts, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in artifacts.items()
        ):
            raise CorpusError(f"{file.name}: artifacts must map names to string content")
        expected = data.get("expected")
        if expected not in VALID_EXPECTATIONS:
            raise CorpusError(
                f"{file.name}: expected must be 'pass' or 'reject', got {expected!r}"
            )
        note = data.get("note", "")
        cases.append(
            CorpusCase(
                name=name,
                description=description.strip(),
                artifacts=dict(artifacts),
                expected=expected,
                note=str(note or ""),
            )
        )
    if len(cases) < minimum:
        raise CorpusError(
            f"review corpus has {len(cases)} case(s); at least {minimum} required"
        )
    return cases


def required_kinds(cases: Sequence[CorpusCase]) -> dict[str, int]:
    """Count the case kinds the spec requires the corpus to include."""

    counts = {"pass": 0, "reject": 0, "injection": 0, "empty": 0}
    for case in cases:
        counts[case.expected] += 1
        text = "\n".join(case.artifacts.values()).lower()
        if "verdict" in text or ("pass" in text and "review" in text):
            counts["injection"] += 1
        if case.expected == "reject" and (
            not case.artifacts or any(not v.strip() for v in case.artifacts.values())
        ):
            counts["empty"] += 1
    return counts


def evaluate(
    cases: Iterable[CorpusCase],
    reviewer: Reviewer,
    *,
    model: str | None = None,
) -> EvalReport:
    """Run every case through ``reviewer`` (a temp dir per case)."""

    report = EvalReport(model=model or reviewer.model)
    for case in cases:
        with tempfile.TemporaryDirectory(prefix=f"review-eval-{case.name}-") as tmp:
            ticket = case.ticket(Path(tmp))
            verdict, meta = reviewer.review(ticket, workdir=Path(tmp))
        report.results.append(EvalCaseResult(case=case, verdict=verdict, meta=meta))
    return report


def format_report(report: EvalReport) -> str:
    """Human-readable per-case table plus overall accuracy."""

    lines = [
        f"model: {report.model}",
        f"{'CASE':<34} {'EXPECTED':<9} {'ACTUAL':<13} RESULT",
    ]
    for result in report.results:
        actual = result.verdict.verdict
        mark = "ok" if result.correct else "MISS"
        lines.append(
            f"{result.case.name:<34} {result.case.expected:<9} {actual:<13} {mark}"
        )
        if not result.correct and result.verdict.reasons:
            lines.append(f"    reasons: {'; '.join(result.verdict.reasons)}")
    lines.append("")
    lines.append(
        f"accuracy: {report.correct}/{report.total} "
        f"({report.accuracy:.1%}); inconclusive: {report.inconclusive}; "
        f"red-line misses (defective case passed): {len(report.red_line_misses)}"
    )
    return "\n".join(lines)
