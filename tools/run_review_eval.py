#!/usr/bin/env python3
"""Run the phase-5 review corpus against configured review model(s).

Offline, out-of-band tool -- NOT part of the unit test suite.  It is where the
Revision-4 A/B comparison happens: no second reviewer runs in production.

    python3 tools/run_review_eval.py                       # review_model
    python3 tools/run_review_eval.py --models A B          # A/B offline
    python3 tools/run_review_eval.py --corpus path/to/dir --json out.json

The endpoint (base URL + token) is resolved exactly like the Brain's: from the
``env`` block of ``~/.claude/settings.json`` (or the live environment).  No key
is read from, or written to, this repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite.config import load_config  # noqa: E402
from superagent_lite.planner import (  # noqa: E402
    MessagesClient,
    PlannerError,
    load_endpoint,
)
from superagent_lite.review_eval import (  # noqa: E402
    CorpusError,
    evaluate,
    format_report,
    load_corpus,
    required_kinds,
)
from superagent_lite.reviewer import Reviewer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config", default=None, help="config.json (default: ./config.json, else defaults)"
    )
    parser.add_argument(
        "--corpus", default=None, help="corpus directory (default tests/fixtures/review_corpus)"
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="model name(s) to evaluate; default: config review_model, "
        "else review_shadow_model",
    )
    parser.add_argument("--json", default=None, metavar="PATH", help="write a JSON report")
    parser.add_argument(
        "--show-prompt", action="store_true", help="print the first case's prompt"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, require=args.config is not None)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in config.get("_warnings") or []:
        print(f"warning: {warning}", file=sys.stderr)

    models = args.models or [
        model
        for model in (config.get("review_model"), config.get("review_shadow_model"))
        if model
    ]
    if not models:
        print(
            "error: no review model configured; pass --models MODEL or set "
            "review_model in the config",
            file=sys.stderr,
        )
        return 2

    try:
        cases = load_corpus(args.corpus)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    kinds = required_kinds(cases)
    print(
        f"corpus: {len(cases)} case(s) "
        f"(expected pass={kinds['pass']}, reject={kinds['reject']}, "
        f"injection={kinds['injection']}, empty={kinds['empty']})"
    )

    try:
        endpoint = load_endpoint(config["brain_settings_path"])
    except PlannerError as exc:
        print(f"error: cannot resolve the model endpoint: {exc}", file=sys.stderr)
        return 2
    transport = MessagesClient(
        endpoint, timeout=float(config["review_timeout_seconds"])
    )

    reports = []
    for model in models:
        reviewer = Reviewer(
            model,
            transport,
            max_artifact_bytes=int(config["review_max_artifact_bytes"]),
            max_total_bytes=int(config["review_max_total_bytes"]),
            timeout=float(config["review_timeout_seconds"]),
            max_tokens=int(config["review_max_tokens"]),
        )
        report = evaluate(cases, reviewer)
        reports.append(report)
        print()
        print(format_report(report))
        if args.show_prompt and report.results:
            prompt = report.results[0].meta.get("prompt")
            if prompt:
                print("\n--- first prompt ---")
                print(prompt)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "model": report.model,
                        "accuracy": report.accuracy,
                        "correct": report.correct,
                        "total": report.total,
                        "inconclusive": report.inconclusive,
                        "red_line_misses": [r.case.name for r in report.red_line_misses],
                        "cases": [
                            {
                                "name": r.case.name,
                                "expected": r.case.expected,
                                "actual": r.verdict.verdict,
                                "reasons": r.verdict.reasons,
                            }
                            for r in report.results
                        ],
                    }
                    for report in reports
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
