#!/usr/bin/env python3
"""Phase 5 tests: semantic review, approval gate, decision log, eval corpus.

Everything here is offline.  Reviewer model calls go through a fake transport
(:class:`FakeReviewTransport`); the gate and decision log are exercised
against a real SQLite store in a temp runtime directory.  No credentials, no
network, no Claude Code binary.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite.cli import main as cli_main  # noqa: E402
from superagent_lite.config import (  # noqa: E402
    DEFAULT_GATE_PATTERNS,
    normalize_config,
)
from superagent_lite.decision_log import (  # noqa: E402
    GENESIS_PREV_HASH,
    DecisionLog,
    canonical_json,
    open_decision_log,
    verify_chain,
)
from superagent_lite.http_api import needs_attention  # noqa: E402
from superagent_lite.models import (  # noqa: E402
    REVIEW_INCONCLUSIVE,
    REVIEW_PASSED,
    REVIEW_REJECTED,
    REVIEW_REJECTED_ONCE,
    Ticket,
    TicketStatus,
)
from superagent_lite.review_eval import (  # noqa: E402
    DEFAULT_CORPUS_DIR,
    CorpusError,
    evaluate,
    format_report,
    load_corpus,
    required_kinds,
)
from superagent_lite.reviewer import (  # noqa: E402
    PROMPT_TEMPLATE_VERSION,
    REASON_MISSING_ARTIFACT,
    REASON_REDACTION,
    REASON_TIMEOUT,
    REASON_UNPARSEABLE,
    ReviewContext,
    Reviewer,
    build_review_prompt,
    parse_verdict,
    sanitize_artifact_name,
    should_review,
)
from superagent_lite.router import Router  # noqa: E402
from superagent_lite.store import approval_content_hash  # noqa: E402
from superagent_lite.workers.base import Worker, WorkerResult  # noqa: E402


def verdict_reply(verdict: str, reasons=(), evidence=()) -> str:
    return "Reasoning about the artifacts.\nVERDICT " + json.dumps(
        {"verdict": verdict, "reasons": list(reasons), "evidence": list(evidence)}
    )


class FakeReviewTransport:
    """A Messages-compatible transport that answers from a script.

    ``answers`` is a list of replies returned in order (the last one repeats);
    an entry that is an exception instance is raised instead, and ``"echo"``
    returns a verdict derived from the prompt's artifact content.
    """

    def __init__(self, *answers, record: bool = True) -> None:
        self.answers = list(answers) or [verdict_reply("pass")]
        self.calls: list[dict] = []
        self.record = record
        self._lock = threading.Lock()

    def complete(self, *, model, system, messages, max_tokens=1024, temperature=None):
        with self._lock:
            index = len(self.calls)
            if self.record:
                self.calls.append(
                    {
                        "model": model,
                        "system": system,
                        "messages": messages,
                        "max_tokens": max_tokens,
                    }
                )
        answer = self.answers[min(index, len(self.answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def prompts(self) -> list[str]:
        return [call["messages"][0]["content"] for call in self.calls]


class TranscriptWorker(Worker):
    """Writes one file whose content each run can differ from the last."""

    result_contract = False

    def __init__(self, contents=("correct content",), name: str = "writer") -> None:
        self.name = name
        self.contents = list(contents)
        self.calls = 0
        self.descriptions: list[str] = []

    def run(self, ticket: Ticket, workdir: Path) -> WorkerResult:
        self.descriptions.append(ticket.description)
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        (Path(workdir) / "out.txt").write_text(content, encoding="utf-8")
        return WorkerResult(
            success=True,
            summary="wrote out.txt",
            exit_code=0,
            artifacts=["out.txt"],
        )


class Phase5Case(unittest.TestCase):
    """Temp runtime directory plus a Router factory with a fake reviewer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.transport = FakeReviewTransport()
        self._routers: list[Router] = []

    def tearDown(self) -> None:
        for router in self._routers:
            router.close()
        self._tmp.cleanup()

    def config(self, **overrides) -> dict:
        config = {
            "runtime_dir": str(self.runtime),
            "default_worker": "echo",
            "claude_model": None,
            "timeout_seconds": 10,
            "stale_seconds": 60,
            "heartbeat_seconds": 0.5,
            "review_mode": "shadow",
            "review_model": "reviewer-model",
            "review_enabled": True,
        }
        config.update(overrides)
        return config

    def router(self, *, workers=None, transport=None, **overrides) -> Router:
        router = Router(
            config=self.config(**overrides),
            workers={w.name: w for w in (workers or [])},
            recover_mode="none",
            review_transport=transport if transport is not None else self.transport,
        )
        self._routers.append(router)
        return router

    def events(self, router: Router, name: str | None = None) -> list[dict]:
        records = router.decision_log.read()
        return [r for r in records if name is None or r["event"] == name]


# ---------------------------------------------------------------------------
# 1. Selection (deterministic code, never a model)
# ---------------------------------------------------------------------------
class SelectionTests(unittest.TestCase):
    def ticket(self, worker: str = "claude") -> Ticket:
        return Ticket(id="t_1", description="do it", worker=worker)

    def test_non_echo_worker_is_reviewed_and_echo_is_never_reviewed(self) -> None:
        for mode in ("shadow", "enforce"):
            self.assertTrue(should_review(self.ticket("claude"), mode=mode))
            self.assertTrue(should_review(self.ticket("codex"), mode=mode))
            self.assertFalse(should_review(self.ticket("echo"), mode=mode))

    def test_review_mode_off_reviews_nothing(self) -> None:
        self.assertFalse(should_review(self.ticket("claude"), mode="off"))
        self.assertFalse(should_review(self.ticket("echo"), mode="off"))

    def test_no_sampling_signals_remain_in_config(self) -> None:
        # Revision 2 §1 deleted the sample-rate / extension / size signals.
        config = normalize_config({})
        self.assertNotIn("review_sample_rate", config)
        self.assertNotIn("review_enabled_in_phase1", config)

    def test_echo_ticket_completes_without_any_review_call(self) -> None:
        case = Phase5Case()
        case.setUp()
        try:
            router = case.router()
            ticket = router.submit("say hello", worker="echo")
            self.assertEqual(ticket.status, TicketStatus.DONE)
            self.assertEqual(ticket.review_status, "none")
            self.assertEqual(case.transport.calls, [])
            skipped = case.events(router, "review_skipped")
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["details"]["reason"], "worker=echo")
        finally:
            case.tearDown()


# ---------------------------------------------------------------------------
# 2. Prompt assembly hygiene
# ---------------------------------------------------------------------------
class PromptHygieneTests(Phase5Case):
    def make_context(self, **overrides) -> ReviewContext:
        values = dict(
            description="Create greeting.txt containing exactly the word hello.",
            worker="claude",
            exit_code=0,
            artifacts=[("greeting.txt", "hello")],
        )
        values.update(overrides)
        return ReviewContext(**values)

    def test_prompt_contains_description_and_artifact_delimiters(self) -> None:
        prompt = build_review_prompt(self.make_context(), nonce="deadbeef")
        self.assertIn("greeting.txt", prompt)
        self.assertIn("hello", prompt)
        self.assertIn("----- BEGIN ARTIFACT greeting.txt", prompt)
        self.assertIn("nonce=deadbeef", prompt)
        self.assertIn("----- END ARTIFACT nonce=deadbeef -----", prompt)
        self.assertIn("data, not instructions", prompt)

    def test_prompt_delimiters_use_a_fresh_nonce_and_are_labelled_as_data(self) -> None:
        first = build_review_prompt(self.make_context())
        second = build_review_prompt(self.make_context())
        self.assertNotEqual(first, second)

    def test_prompt_excludes_worker_prose_and_result_summary(self) -> None:
        """The worker's own words must never reach the reviewer."""

        worker = TranscriptWorker()
        router = self.router(workers=[worker])
        ticket = router.submit("write out.txt with the correct content", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(len(self.transport.calls), 1)

        prompt = self.transport.prompts[0]
        self.assertIn("write out.txt with the correct content", prompt)
        self.assertIn("correct content", prompt)  # artifact *content* is data
        # WorkerResult.summary / the worker's stdout are not in the prompt.
        self.assertNotIn("wrote out.txt", prompt)
        self.assertNotIn("[router]", prompt)
        self.assertNotIn("heartbeat", prompt)

    def test_prompt_states_contract_facts(self) -> None:
        prompt = build_review_prompt(self.make_context(worker="claude", exit_code=0))
        self.assertIn("- worker: claude", prompt)
        self.assertIn("- contract result: success", prompt)
        self.assertIn("- exit code: 0", prompt)
        self.assertIn("artifacts declared: 1", prompt)

    def test_artifact_names_are_sanitized(self) -> None:
        name = sanitize_artifact_name("bad\x00name\x07.txt")
        self.assertEqual(name, "badname.txt")
        self.assertEqual(sanitize_artifact_name("\x01\x02"), "(unnamed)")

    def test_artifacts_are_byte_capped_per_file_and_in_total(self) -> None:
        workdir = self.root / "workdir"
        workdir.mkdir()
        # ``Q`` and ``J`` never occur in the prompt template, so counting them
        # measures exactly how much artifact content was sent.
        (workdir / "big.txt").write_text("Q" * 5000, encoding="utf-8")
        (workdir / "second.txt").write_text("J" * 5000, encoding="utf-8")
        ticket = Ticket(
            id="t_cap",
            description="produce two files",
            worker="claude",
            workdir=str(workdir),
            artifacts=["big.txt", "second.txt"],
        )
        reviewer = Reviewer(
            "model",
            self.transport,
            max_artifact_bytes=1000,
            max_total_bytes=1500,
        )
        verdict, meta = reviewer.review(ticket, workdir=workdir)
        self.assertEqual(verdict.verdict, "pass")
        prompt = self.transport.prompts[0]
        self.assertLessEqual(prompt.count("Q"), 1000)  # per-file cap
        self.assertLessEqual(prompt.count("J"), 500)  # remaining total budget
        self.assertGreaterEqual(prompt.count("J"), 1)
        self.assertIn("(truncated)", prompt)
        # ``artifact_bytes`` counts the text actually sent, including the two
        # "\n...(truncated)" markers the cap appends.
        self.assertLessEqual(meta["artifact_bytes"], 1500 + 2 * len("\n...(truncated)"))

    def test_missing_artifact_rejects_without_calling_the_model(self) -> None:
        workdir = self.root / "workdir-missing"
        workdir.mkdir()
        ticket = Ticket(
            id="t_missing",
            description="create greeting.txt",
            worker="claude",
            workdir=str(workdir),
            artifacts=["greeting.txt"],
        )
        reviewer = Reviewer("model", self.transport)
        verdict, meta = reviewer.review(ticket, workdir=workdir)
        self.assertEqual(verdict.verdict, "reject")
        self.assertEqual(verdict.error, REASON_MISSING_ARTIFACT)
        self.assertEqual(verdict.reasons, [REASON_MISSING_ARTIFACT])
        self.assertFalse(meta["model_called"])
        self.assertEqual(self.transport.calls, [])

    def test_redaction_failure_skips_the_review_instead_of_sending_raw_text(self) -> None:
        workdir = self.root / "workdir-redact"
        workdir.mkdir()
        (workdir / "a.txt").write_text("secret content", encoding="utf-8")
        ticket = Ticket(
            id="t_redact",
            description="produce a.txt",
            worker="claude",
            workdir=str(workdir),
            artifacts=["a.txt"],
        )
        reviewer = Reviewer("model", self.transport)
        import superagent_lite.reviewer as reviewer_module

        original = reviewer_module.redact_with_count

        def broken(text, env=None):
            raise RuntimeError("redaction exploded")

        reviewer_module.redact_with_count = broken
        try:
            verdict, meta = reviewer.review(ticket, workdir=workdir)
        finally:
            reviewer_module.redact_with_count = original
        self.assertEqual(verdict.verdict, "inconclusive")
        self.assertEqual(verdict.error, REASON_REDACTION)
        self.assertEqual(self.transport.calls, [])

    def test_artifact_content_is_redacted_before_it_is_sent(self) -> None:
        workdir = self.root / "workdir-secret"
        workdir.mkdir()
        (workdir / "notes.txt").write_text(
            "api key is sk-abcdef123456 and token is super-secret-value",
            encoding="utf-8",
        )
        ticket = Ticket(
            id="t_secret",
            description="write notes.txt",
            worker="claude",
            workdir=str(workdir),
            artifacts=["notes.txt"],
        )
        env = {"SOME_API_TOKEN": "super-secret-value"}
        reviewer = Reviewer("model", self.transport, env=env)
        verdict, meta = reviewer.review(ticket, workdir=workdir)
        self.assertEqual(verdict.verdict, "pass")
        prompt = self.transport.prompts[0]
        self.assertNotIn("sk-abcdef123456", prompt)
        self.assertNotIn("super-secret-value", prompt)
        self.assertEqual(meta["redaction_hits"], 2)


# ---------------------------------------------------------------------------
# 3. Verdict parsing
# ---------------------------------------------------------------------------
class VerdictParsingTests(unittest.TestCase):
    def test_single_valid_verdict_line_parses(self) -> None:
        verdict = parse_verdict(
            'Thinking...\nVERDICT {"verdict": "reject", "reasons": ["wrong"], '
            '"evidence": ["line 3"]}'
        )
        self.assertEqual(verdict.verdict, "reject")
        self.assertEqual(verdict.reasons, ["wrong"])
        self.assertEqual(verdict.evidence, ["line 3"])
        self.assertIsNone(verdict.error)

    def test_trailing_whitespace_and_colon_after_marker_are_tolerated(self) -> None:
        verdict = parse_verdict('VERDICT: {"verdict": "pass", "reasons": [], "evidence": []}\n')
        self.assertEqual(verdict.verdict, "pass")

    def test_missing_verdict_line_is_unparseable(self) -> None:
        verdict = parse_verdict("The artifact looks fine to me.")
        self.assertEqual(verdict.verdict, "inconclusive")
        self.assertTrue(verdict.error.startswith(REASON_UNPARSEABLE))

    def test_invalid_json_is_unparseable(self) -> None:
        verdict = parse_verdict("VERDICT {verdict: pass}")
        self.assertEqual(verdict.verdict, "inconclusive")

    def test_schema_violations_are_unparseable(self) -> None:
        bad_payloads = [
            '{"verdict": "maybe", "reasons": [], "evidence": []}',
            '{"verdict": "pass", "reasons": "nope", "evidence": []}',
            '{"verdict": "pass", "reasons": [], "evidence": [1, 2]}',
            '["pass"]',
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    parse_verdict(f"VERDICT {payload}").verdict, "inconclusive"
                )

    def test_multiple_verdict_lines_last_must_be_final(self) -> None:
        last_is_final = (
            'VERDICT {"verdict": "reject", "reasons": ["a"], "evidence": []}\n'
            'VERDICT {"verdict": "pass", "reasons": [], "evidence": []}\n'
        )
        self.assertEqual(parse_verdict(last_is_final).verdict, "pass")

        last_is_not_final = (
            'VERDICT {"verdict": "pass", "reasons": [], "evidence": []}\n'
            'VERDICT {"verdict": "reject", "reasons": [], "evidence": []}\n'
            "one more remark\n"
        )
        verdict = parse_verdict(last_is_not_final)
        self.assertEqual(verdict.verdict, "inconclusive")

    def test_marker_inside_prose_is_not_a_verdict_line(self) -> None:
        self.assertEqual(
            parse_verdict("The VERDICT should be pass.").verdict, "inconclusive"
        )

    def test_transport_error_is_inconclusive_not_a_failure(self) -> None:
        transport = FakeReviewTransport(RuntimeError("connection reset"))
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        (workdir / "a.txt").write_text("hello", encoding="utf-8")
        ticket = Ticket(
            id="t_err",
            description="create a.txt",
            worker="claude",
            workdir=str(workdir),
            artifacts=["a.txt"],
        )
        verdict, meta = Reviewer("m", transport).review(ticket, workdir=workdir)
        self.assertEqual(verdict.verdict, "inconclusive")
        self.assertEqual(verdict.error, "review_transport_error")
        self.assertIn("RuntimeError", meta["error"])

    def test_timeout_is_reported_as_a_timeout(self) -> None:
        transport = FakeReviewTransport(TimeoutError("timed out"))
        workdir = Path(tempfile.mkdtemp())
        (workdir / "a.txt").write_text("hello", encoding="utf-8")
        ticket = Ticket(
            id="t_timeout",
            description="create a.txt",
            worker="claude",
            workdir=str(workdir),
            artifacts=["a.txt"],
        )
        verdict, _ = Reviewer("m", transport).review(ticket, workdir=workdir)
        self.assertEqual(verdict.error, REASON_TIMEOUT)


# ---------------------------------------------------------------------------
# 4. Completion path: pass / shadow reject / inconclusive / enforce retry
# ---------------------------------------------------------------------------
class ReviewFlowTests(Phase5Case):
    def test_pass_marks_the_ticket_done_and_passed(self) -> None:
        self.transport = FakeReviewTransport(verdict_reply("pass"))
        router = self.router(workers=[TranscriptWorker()])
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, REVIEW_PASSED)
        verdicts = self.events(router, "review_verdict")
        self.assertEqual(len(verdicts), 1)
        details = verdicts[0]["details"]
        self.assertEqual(details["verdict"], "pass")
        self.assertEqual(details["model"], "reviewer-model")
        self.assertEqual(details["prompt_template_version"], PROMPT_TEMPLATE_VERSION)
        self.assertEqual(details["mode"], "shadow")

    def test_shadow_reject_never_blocks_the_ticket(self) -> None:
        self.transport = FakeReviewTransport(
            verdict_reply("reject", ["content is wrong"])
        )
        router = self.router(workers=[TranscriptWorker()])
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, REVIEW_REJECTED)
        self.assertTrue(self.events(router, "review_shadow_reject"))
        self.assertFalse(self.events(router, "review_retry"))

    def test_missing_artifact_is_a_deterministic_reject_without_a_model_call(self) -> None:
        class LiarWorker(Worker):
            name = "liar"
            result_contract = False

            def run(self, ticket, workdir):
                return WorkerResult(
                    success=True,
                    summary="wrote out.txt",
                    exit_code=0,
                    artifacts=["out.txt"],
                )

        router = self.router(
            workers=[LiarWorker()], review_mode="enforce", review_max_retries=0
        )
        ticket = router.submit("write out.txt", worker="liar")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertEqual(ticket.review_status, REVIEW_REJECTED)
        self.assertIn(REASON_MISSING_ARTIFACT, ticket.error)

    def test_inconclusive_completes_on_the_contract_result(self) -> None:
        self.transport = FakeReviewTransport("no verdict here at all")
        router = self.router(workers=[TranscriptWorker()])
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, REVIEW_INCONCLUSIVE)
        self.assertTrue(self.events(router, "review_inconclusive"))

    def test_reviewer_crash_does_not_fail_the_ticket(self) -> None:
        class ExplodingReviewer:
            model = "boom"

            def review(self, *args, **kwargs):
                raise RuntimeError("reviewer bug")

        router = self.router(workers=[TranscriptWorker()])
        router.reviewer = ExplodingReviewer()  # type: ignore[assignment]
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, REVIEW_INCONCLUSIVE)

    def test_enforce_reject_requeues_once_then_fails(self) -> None:
        self.transport = FakeReviewTransport(
            verdict_reply("reject", ["out.txt says the wrong thing"]),
            verdict_reply("reject", ["still wrong"]),
        )
        worker = TranscriptWorker()
        router = self.router(
            workers=[worker],
            review_mode="enforce",
            review_max_retries=1,
        )
        first = router.submit("write the correct out.txt", worker="writer")
        self.assertEqual(first.status, TicketStatus.PENDING)
        self.assertEqual(first.review_status, REVIEW_REJECTED_ONCE)
        self.assertEqual(first.review_attempts, 1)
        self.assertIn("[Review feedback", first.description)
        self.assertIn("out.txt says the wrong thing", first.description)
        self.assertTrue(self.events(router, "review_retry"))

        second = router.dispatch(first.id)
        self.assertEqual(second.status, TicketStatus.FAILED)
        self.assertEqual(second.review_status, REVIEW_REJECTED)
        self.assertEqual(second.review_attempts, 2)
        self.assertIn("review rejected", second.error)
        self.assertIn("still wrong", second.error)
        self.assertEqual(worker.calls, 2)

    def test_enforce_pass_completes_after_no_retry(self) -> None:
        self.transport = FakeReviewTransport(verdict_reply("pass"))
        router = self.router(workers=[TranscriptWorker()], review_mode="enforce")
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, REVIEW_PASSED)

    def test_review_feedback_is_sanitized_and_capped(self) -> None:
        reasons = ["bad\x00thing\x1b[31m " + "x" * 3000]
        self.transport = FakeReviewTransport(verdict_reply("reject", reasons))
        router = self.router(
            workers=[TranscriptWorker()],
            review_mode="enforce",
            review_max_retries=1,
            review_max_feedback_chars=200,
        )
        ticket = router.submit("write out.txt", worker="writer")
        feedback = ticket.description.split("[Review feedback", 1)[1]
        self.assertNotIn("\x00", feedback)
        self.assertNotIn("\x1b", feedback)
        # The freshly appended block holds at most the configured 200 chars.
        block = feedback.split("Address these reasons:\n", 1)[1]
        self.assertLess(len(block), 300)
        # Reasons are stored raw on the row (the description holds the
        # sanitized form only).
        self.assertEqual(ticket.review_reasons, reasons)
        self.assertIn("bad\x00thing", ticket.review_reasons[0])

    def test_execution_failure_is_skipped_and_logged(self) -> None:
        class FailingWorker(Worker):
            name = "failer"
            result_contract = False

            def run(self, ticket, workdir):
                return WorkerResult(success=False, error="simulated failure")

        router = self.router(workers=[FailingWorker()])
        ticket = router.submit("do the thing", worker="failer")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertEqual(ticket.review_status, "none")
        skipped = self.events(router, "review_skipped")
        self.assertEqual(skipped[-1]["details"]["reason"], "execution_failed")
        self.assertEqual(self.transport.calls, [])

    def test_mode_off_logs_skipped_reviews_so_there_are_no_silent_gaps(self) -> None:
        router = self.router(workers=[TranscriptWorker()], review_mode="off")
        router.reviewer = None
        ticket = router.submit("write out.txt", worker="writer")
        self.assertEqual(ticket.status, TicketStatus.DONE)
        self.assertEqual(ticket.review_status, "none")
        skipped = self.events(router, "review_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["details"]["reason"], "review_disabled")
        self.assertEqual(self.transport.calls, [])

    def test_review_model_must_differ_from_planner_to_warn(self) -> None:
        config = normalize_config(
            {
                "review_mode": "shadow",
                "review_model": "same-model",
                "brain_model": "same-model",
            }
        )
        self.assertTrue(
            any("equals brain_model" in warning for warning in config["_warnings"])
        )

    def test_review_is_disabled_loudly_when_no_model_is_configured(self) -> None:
        config = normalize_config({"review_mode": "enforce", "review_model": None})
        self.assertEqual(config["review_mode"], "off")
        self.assertTrue(
            any("review_model is null" in w for w in config["_warnings"])
        )

    def test_review_timeout_is_clamped_below_stale_seconds(self) -> None:
        config = normalize_config(
            {
                "review_mode": "shadow",
                "review_model": "m",
                "stale_seconds": 60,
                "review_timeout_seconds": 90,
            }
        )
        self.assertLess(config["review_timeout_seconds"], 60)
        self.assertTrue(
            any("clamped" in w for w in config["_warnings"])
        )


# ---------------------------------------------------------------------------
# 5. Approval gate: pattern matching and CAS transitions
# ---------------------------------------------------------------------------
class GatePatternTests(Phase5Case):
    def test_default_patterns_cover_irreversible_actions(self) -> None:
        router = self.router()
        for description in (
            "delete the old logs",
            "rm the file",
            "请删除这批文件",
            "publish the release",
            "deploy to production",
            "send an email to the team",
            "发送邮件给团队",
            "pay the invoice",
            "支付账单",
            "git push origin main",
        ):
            with self.subTest(description=description):
                self.assertTrue(router.is_gated(description))

    def test_shipped_defaults_are_the_documented_ones(self) -> None:
        # A regression here silently widens or narrows the gate for every
        # install, so pin the shipped list itself, not only its behaviour.
        config = normalize_config({})
        self.assertEqual(config["gate_patterns"], list(DEFAULT_GATE_PATTERNS))
        self.assertTrue(config["gate_enabled"])

    def test_benign_descriptions_are_not_gated(self) -> None:
        router = self.router()
        for description in (
            "create greeting.txt containing hello",
            "read the config file",
            "summarise this article",
        ):
            with self.subTest(description=description):
                self.assertFalse(router.is_gated(description))

    def test_gate_can_be_disabled_and_patterns_overridden(self) -> None:
        self.assertFalse(self.router(gate_enabled=False).is_gated("delete everything"))
        custom = self.router(gate_patterns=[r"only-this"])
        self.assertTrue(custom.is_gated("ONLY-THIS now"))
        self.assertFalse(custom.is_gated("delete everything"))

    def test_invalid_gate_regex_is_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            normalize_config({"gate_patterns": ["(unclosed"]})

    def test_gated_ticket_starts_awaiting_approval_and_records_intent(self) -> None:
        router = self.router()
        ticket = router.enqueue("delete the temporary files", worker="echo")
        self.assertEqual(ticket.status, TicketStatus.AWAITING_APPROVAL)
        self.assertEqual(ticket.approval_hash, approval_content_hash(ticket.description))
        self.assertTrue(self.events(router, "approval_requested"))
        created = self.events(router, "ticket_created")[-1]
        self.assertTrue(created["details"]["gated"])

    def test_daemon_never_claims_awaiting_approval(self) -> None:
        router = self.router()
        gated = router.enqueue("deploy the service", worker="echo")
        self.assertIsNone(router.store.claim_next())
        # ``dispatch`` falls back to returning the untouched row when the
        # claim loses the CAS, so assert on the state rather than on None.
        dispatched = router.dispatch(gated.id)
        self.assertEqual(dispatched.status, TicketStatus.AWAITING_APPROVAL)
        self.assertEqual(router.get(gated.id).status, TicketStatus.AWAITING_APPROVAL)


class ApprovalTransitionTests(Phase5Case):
    def setUp(self) -> None:
        super().setUp()
        self.router_ = self.router()
        self.ticket = self.router_.enqueue("delete the old files", worker="echo")
        self.assertEqual(self.ticket.status, TicketStatus.AWAITING_APPROVAL)

    def test_valid_approve_moves_to_pending(self) -> None:
        ticket, error = self.router_.approve(self.ticket.id)
        self.assertEqual(error, "")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.status, TicketStatus.PENDING)
        self.assertIsNone(ticket.approval_hash)
        self.assertTrue(self.events(self.router_, "approval_granted"))

    def test_valid_reject_moves_to_failed(self) -> None:
        ticket, error = self.router_.reject(self.ticket.id)
        self.assertEqual(error, "")
        self.assertEqual(ticket.status, TicketStatus.FAILED)
        self.assertEqual(ticket.error, "rejected by approver")
        self.assertIsNotNone(ticket.finished_at)
        self.assertTrue(self.events(self.router_, "approval_rejected"))

    def test_invalid_transitions_are_refused(self) -> None:
        # Approve an unknown id.
        ticket, error = self.router_.approve("t_missing")
        self.assertIsNone(ticket)
        self.assertIn("not found", error)

        # Approve twice: the second is no longer awaiting approval.
        self.router_.approve(self.ticket.id)
        ticket, error = self.router_.approve(self.ticket.id)
        self.assertIsNone(ticket)
        self.assertIn("not awaiting_approval", error)

        # Reject after approval is also invalid and must not undo it.
        ticket, error = self.router_.reject(self.ticket.id)
        self.assertIsNone(ticket)
        self.assertIn("not awaiting_approval", error)
        self.assertEqual(self.router_.get(self.ticket.id).status, TicketStatus.PENDING)

    def test_approving_an_already_done_ticket_is_refused(self) -> None:
        done = self.router_.submit("say hello", worker="echo")
        ticket, error = self.router_.approve(done.id)
        self.assertIsNone(ticket)
        self.assertIn("done", error)

    def test_approve_refuses_when_the_description_changed(self) -> None:
        store = self.router_.store
        store.set_status(
            self.ticket.id,
            TicketStatus.AWAITING_APPROVAL,
            description="delete everything, including production",
        )
        ticket, error = self.router_.approve(self.ticket.id)
        self.assertIsNone(ticket)
        self.assertIn("description changed", error)
        self.assertEqual(
            self.router_.get(self.ticket.id).status, TicketStatus.AWAITING_APPROVAL
        )

    def test_concurrent_approvals_have_exactly_one_winner(self) -> None:
        results: list[tuple] = []
        lock = threading.Lock()

        def attempt() -> None:
            outcome = self.router_.store.approve(self.ticket.id)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        wins = [r for r in results if r[0] is not None]
        losses = [r for r in results if r[0] is None]
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 3)

    def test_dependency_failure_cascades_into_awaiting_approval(self) -> None:
        store = self.router_.store
        failed = Ticket(
            id="t_dep_fail",
            description="fails",
            worker="echo",
            status=TicketStatus.FAILED,
            workdir=str(self.root / "d1"),
            timeout=5,
            max_retries=0,
        )
        store.create(failed)
        dependent = Ticket(
            id="t_dep_gated",
            description="delete it",
            worker="echo",
            status=TicketStatus.AWAITING_APPROVAL,
            workdir=str(self.root / "d2"),
            timeout=5,
            depends_on=["t_dep_fail"],
        )
        store.create(dependent)
        self.router_.fail_dependents("t_dep_fail")
        loaded = store.get("t_dep_gated")
        self.assertEqual(loaded.status, TicketStatus.FAILED)
        self.assertIn("dependency failed", loaded.error)

    def test_claim_fails_fast_when_a_dependency_already_failed(self) -> None:
        store = self.router_.store
        store.create(
            Ticket(
                id="t_failed_dep",
                description="x",
                worker="echo",
                status=TicketStatus.FAILED,
                workdir=str(self.root / "d3"),
                timeout=5,
            )
        )
        blocked = store.create(
            Ticket(
                id="t_blocked",
                description="y",
                worker="echo",
                workdir=str(self.root / "d4"),
                timeout=5,
                depends_on=["t_failed_dep"],
            )
        )
        self.assertIsNone(store.claim(blocked.id))
        loaded = store.get(blocked.id)
        self.assertEqual(loaded.status, TicketStatus.FAILED)
        self.assertIn("dependency failed", loaded.error)

    def test_cli_approve_and_reject_work_and_report_errors(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config()), encoding="utf-8")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(
                ["--config", str(config_path), "approve", self.ticket.id]
            )
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("status: pending", out.getvalue())
        self.assertEqual(self.router_.get(self.ticket.id).status, TicketStatus.PENDING)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_main(
                ["--config", str(config_path), "reject", self.ticket.id]
            )
        self.assertEqual(code, 1)
        self.assertIn("not awaiting_approval", err.getvalue())

    def test_cli_approve_does_not_recover_unrelated_stale_tickets(self) -> None:
        victim = self.router_.enqueue("write a file", worker="echo")
        self.router_.store.set_status(
            victim.id,
            TicketStatus.RUNNING,
            claim_token=None,
            heartbeat_at="2000-01-01T00:00:00+00:00",
            started_at="2000-01-01T00:00:00+00:00",
        )
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config()), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli_main(
                ["--config", str(config_path), "approve", self.ticket.id]
            )
        self.assertEqual(code, 0)
        # The stale ticket is untouched: approving is not a recovery sweep.
        self.assertEqual(
            self.router_.get(victim.id).status, TicketStatus.RUNNING
        )

    def test_attention_list_is_read_only_and_surfaces_gated_and_inconclusive(self) -> None:
        inconclusive = Ticket(
            id="t_inconclusive",
            description="was reviewed",
            worker="claude",
            status=TicketStatus.DONE,
            workdir=str(self.root / "w"),
            timeout=5,
            review_status=REVIEW_INCONCLUSIVE,
        )
        entries = needs_attention([self.router_.get(self.ticket.id), inconclusive])
        reasons = {entry["id"]: entry["reason"] for entry in entries}
        self.assertEqual(reasons[self.ticket.id], "awaiting_approval")
        self.assertEqual(reasons["t_inconclusive"], "review_inconclusive")


# ---------------------------------------------------------------------------
# 6. Decision log: append, verify, tamper detection
# ---------------------------------------------------------------------------
class DecisionLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "decisions.jsonl"
        self.log = DecisionLog(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_writes_chained_records(self) -> None:
        self.log.append("ticket_created", ticket_id="t_1", details={"origin": "cli"})
        self.log.append("review_verdict", ticket_id="t_1", details={"verdict": "pass"})
        records = self.log.read()
        self.assertEqual([r["event"] for r in records], ["ticket_created", "review_verdict"])
        self.assertEqual(records[0]["prev_hash"], GENESIS_PREV_HASH)
        self.assertEqual(records[1]["prev_hash"], records[0]["hash"])
        for record in records:
            self.assertIn("ts", record)
            self.assertIn("details", record)
        for line in self.path.read_text(encoding="utf-8").splitlines():
            self.assertEqual(json.loads(line), json.loads(line))
            self.assertTrue(line.startswith("{"), "one canonical JSON object per line")

    def test_records_are_canonical_json(self) -> None:
        self.log.append("x", ticket_id=None, details={"b": 1, "a": 2})
        raw = self.path.read_text(encoding="utf-8").strip()
        record = json.loads(raw)
        # Sorted keys and compact separators: re-serialising any record body
        # reproduces the line the chain hash was computed over.
        self.assertEqual(canonical_json(record), raw)
        self.assertIn('"a":2,"b":1', raw)

    def test_verify_chain_accepts_a_good_chain(self) -> None:
        for index in range(3):
            self.log.append("event", ticket_id=f"t_{index}", details={"n": index})
        status = verify_chain(self.path)
        self.assertTrue(status.ok, status.error)
        self.assertEqual(status.count, 3)

    def test_verify_detects_a_modified_record(self) -> None:
        self.log.append("ticket_created", ticket_id="t_1", details={"origin": "cli"})
        self.log.append("review_verdict", ticket_id="t_1", details={"verdict": "reject"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        edited = json.loads(lines[1])
        edited["details"]["verdict"] = "pass"
        lines[1] = canonical_json(edited)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        status = verify_chain(self.path)
        self.assertFalse(status.ok)
        self.assertEqual(status.first_bad_line, 2)
        self.assertIn("hash mismatch", status.error)

    def test_verify_detects_a_deleted_middle_record(self) -> None:
        for index in range(3):
            self.log.append("event", ticket_id=f"t_{index}")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        status = verify_chain(self.path)
        self.assertFalse(status.ok)
        self.assertEqual(status.first_bad_line, 2)
        self.assertIn("prev_hash", status.error)

    def test_verify_detects_a_hand_appended_record(self) -> None:
        self.log.append("event", ticket_id="t_1")
        forged = {
            "ts": "2026-01-01T00:00:00+00:00",
            "event": "approval_granted",
            "ticket_id": "t_1",
            "details": {},
            "prev_hash": "0" * 64,
            "hash": "0" * 64,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(forged) + "\n")
        status = verify_chain(self.path)
        self.assertFalse(status.ok)
        self.assertEqual(status.first_bad_line, 2)

    def test_a_partial_last_line_is_truncated_before_the_next_append(self) -> None:
        self.log.append("event", ticket_id="t_1")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"ts": "2026-01-01T00:00:00", "event": "half')
        self.log.append("event", ticket_id="t_2")
        status = verify_chain(self.path)
        self.assertTrue(status.ok, status.error)
        self.assertEqual([r["ticket_id"] for r in status.records], ["t_1", "t_2"])

    def test_write_failures_are_counted_and_never_raise(self) -> None:
        blocker = Path(self._tmp.name) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        broken = DecisionLog(blocker / "decisions.jsonl")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = broken.append("event", ticket_id="t_1")
        self.assertIsNone(result)
        self.assertEqual(broken.failures, 1)
        self.assertIn("[decision-log] write failed", stderr.getvalue())

    def test_disabled_log_is_a_noop(self) -> None:
        disabled = open_decision_log(self.path.parent, enabled=False)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.append("event", ticket_id="t_1"))
        self.assertFalse(self.path.exists())

    def test_concurrent_appends_keep_the_chain_intact(self) -> None:
        def writer(index: int) -> None:
            log = DecisionLog(self.path)
            for item in range(10):
                log.append("event", ticket_id=f"t_{index}", details={"item": item})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        status = verify_chain(self.path)
        self.assertTrue(status.ok, status.error)
        self.assertEqual(status.count, 40)

    def test_details_are_capped(self) -> None:
        self.log.append("event", details={"huge": "x" * 5000})
        record = self.log.read()[0]
        self.assertLess(len(record["details"]["huge"]), 2100)
        self.assertTrue(record["details"]["huge"].endswith("...(truncated)"))

    def test_router_events_cover_the_ticket_lifecycle(self) -> None:
        case = Phase5Case()
        case.setUp()
        try:
            case.transport = FakeReviewTransport(verdict_reply("pass"))
            router = case.router(workers=[TranscriptWorker()])
            router.submit("write out.txt", worker="writer")
            router.approve(router.enqueue("delete it", worker="echo").id)
            names = [record["event"] for record in router.decision_log.read()]
            self.assertIn("ticket_created", names)
            self.assertIn("review_verdict", names)
            self.assertIn("ticket_finished", names)
            self.assertIn("approval_requested", names)
            self.assertIn("approval_granted", names)
            self.assertTrue(verify_chain(router.decision_log.path).ok)
        finally:
            case.tearDown()

    def test_cli_decisions_verify_and_tamper_exit_codes(self) -> None:
        runtime = Path(self._tmp.name) / "runtime"
        log = open_decision_log(runtime)
        log.append("ticket_created", ticket_id="t_1")
        config_path = Path(self._tmp.name) / "config.json"
        config_path.write_text(
            json.dumps({"runtime_dir": str(runtime)}), encoding="utf-8"
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--config", str(config_path), "decisions", "--verify"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("ok: 1 record(s) verified", out.getvalue())

        path = runtime / "decisions.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").strip())
        record["event"] = "approval_granted"
        path.write_text(canonical_json(record) + "\n", encoding="utf-8")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["--config", str(config_path), "decisions", "--verify"])
        self.assertEqual(code, 1)
        self.assertIn("first bad line 1", err.getvalue())


# ---------------------------------------------------------------------------
# 7. Eval corpus
# ---------------------------------------------------------------------------
class CorpusTests(unittest.TestCase):
    def test_corpus_loads_at_least_ten_cases(self) -> None:
        cases = load_corpus()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({case.name for case in cases}), len(cases))

    def test_corpus_contains_every_required_kind(self) -> None:
        cases = load_corpus()
        kinds = required_kinds(cases)
        self.assertGreaterEqual(kinds["pass"], 3)
        self.assertGreaterEqual(kinds["reject"], 5)
        self.assertGreaterEqual(kinds["injection"], 2)
        self.assertGreaterEqual(kinds["empty"], 1)
        names = {case.name for case in cases}
        self.assertTrue(any("injection" in name for name in names))
        self.assertTrue(any("missing" in name for name in names))

    def test_injection_cases_expect_opposite_verdicts_for_same_claim(self) -> None:
        by_name = {case.name: case for case in load_corpus()}
        wrong = by_name["07_injection_text_wrong"]
        correct = by_name["08_injection_text_correct"]
        self.assertEqual(wrong.expected, "reject")
        self.assertEqual(correct.expected, "pass")
        self.assertIn(
            "pass", "".join(wrong.artifacts.values()).lower()
        )
        self.assertIn(
            "pass", "".join(correct.artifacts.values()).lower()
        )

    def test_malformed_corpus_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(10):
                (directory / f"{index:02d}.json").write_text(
                    json.dumps(
                        {"description": "d", "artifacts": {}, "expected": "pass"}
                    ),
                    encoding="utf-8",
                )
            (directory / "99_bad.json").write_text(
                json.dumps({"description": "d", "artifacts": {}, "expected": "maybe"}),
                encoding="utf-8",
            )
            with self.assertRaises(CorpusError):
                load_corpus(directory)

    def test_too_few_cases_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CorpusError):
                load_corpus(tmp)

    def test_missing_directory_is_rejected(self) -> None:
        with self.assertRaises(CorpusError):
            load_corpus(Path(DEFAULT_CORPUS_DIR) / "does-not-exist")

    def test_evaluate_uses_the_fake_transport_and_reports_accuracy(self) -> None:
        cases = load_corpus()[:3]
        # A transport that always passes: the two clean cases are right and
        # the defective ones (if selected) are misses.
        transport = FakeReviewTransport(verdict_reply("pass"))
        report = evaluate(cases, Reviewer("fake-model", transport))
        self.assertEqual(report.total, len(cases))
        self.assertEqual(report.model, "fake-model")
        self.assertEqual(len(transport.calls), len(cases))
        self.assertEqual(report.correct, sum(1 for c in cases if c.expected == "pass"))
        text = format_report(report)
        self.assertIn("accuracy:", text)
        self.assertEqual(len(report.red_line_misses), sum(
            1 for c in cases if c.expected == "reject"
        ))

    def test_corpus_case_materialises_artifacts_and_ticket(self) -> None:
        case = next(c for c in load_corpus() if c.name.startswith("01_"))
        with tempfile.TemporaryDirectory() as tmp:
            ticket = case.ticket(Path(tmp))
            self.assertEqual(ticket.worker, "claude")
            self.assertEqual(ticket.artifacts, list(case.artifacts))
            for name, content in case.artifacts.items():
                self.assertEqual((Path(tmp) / name).read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    unittest.main()
