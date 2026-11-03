#!/usr/bin/env python3
"""Tests for the native worker: our own agent loop over the Messages API.

The model is scripted: :class:`FakeClient` returns canned response bodies in
order, so the loop, the tools and the path jail are exercised without any
network access.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite import Endpoint, MessagesClient, NativeWorker, Router  # noqa: E402
from superagent_lite.config import default_config, normalize_config  # noqa: E402
from superagent_lite.models import Ticket  # noqa: E402
from superagent_lite.workers.native import (  # noqa: E402
    JAIL_ERROR,
    MAX_BASH_CHARS,
    MAX_GLOB_ENTRIES,
    MAX_GREP_MATCHES,
    MAX_READ_CHARS,
    SYSTEM_PROMPT,
    TOOLS,
)


def tool_use(name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{name}_{id(arguments)}", "name": name, "input": arguments}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def response(*blocks: dict[str, Any], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": list(blocks), "stop_reason": stop_reason}


SUCCESS_RESULT = (
    'RESULT {"status": "success", "summary": "done", "artifacts": ["answer.txt"]}'
)


class FakeClient:
    """Scripted ``MessagesClient`` stand-in.

    ``answers`` may hold response dicts or exceptions to raise.  Every call's
    keyword arguments are recorded so tests can assert what the model saw.
    """

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def messages(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        answer = self.answers[min(index, len(self.answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class NativeWorkerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.log_path = self.root / "logs" / "ticket.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_ticket(self, description: str = "write answer.txt saying hello") -> Ticket:
        return Ticket(
            id="t_native_test",
            description=description,
            worker="native",
            workdir=str(self.workdir),
            log_path=str(self.log_path),
            timeout=30,
        )

    def make_worker(self, client: Any, **kwargs: Any) -> NativeWorker:
        params = {"max_turns": 6, "bash_timeout_seconds": 10}
        params.update(kwargs)
        return NativeWorker(
            model="test-model",
            settings_path=str(self.root / "settings.json"),
            client=client,
            **params,
        )

    def tool_results(self, call: dict[str, Any]) -> list[dict[str, Any]]:
        """Tool results in the last user message of a recorded call."""

        last = call["messages"][-1]
        self.assertEqual(last["role"], "user")
        return [block for block in last["content"] if block.get("type") == "tool_result"]

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    # -- (a) write_file then final RESULT ------------------------------
    def test_write_file_then_result_succeeds(self) -> None:
        client = FakeClient(
            response(
                text_block("Writing the file."),
                tool_use("write_file", path="answer.txt", content="hello"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket(), self.workdir)

        self.assertTrue(result.success)
        self.assertIn("RESULT", result.output)
        self.assertEqual((self.workdir / "answer.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(result.duration, 0)

        # First call: the task and the tools.  Second: the tool result.
        first, second = client.calls
        self.assertEqual(first["model"], "test-model")
        self.assertEqual(first["tools"], TOOLS)
        self.assertIn("write answer.txt saying hello", first["messages"][0]["content"])
        self.assertIn("RESULT", first["messages"][0]["content"])
        self.assertIn("execution agent", first["system"])
        results = self.tool_results(second)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_error"])
        self.assertIn("answer.txt", results[0]["content"])
        self.assertEqual(second["messages"][1]["role"], "assistant")
        self.assertEqual(second["messages"][1]["content"][1]["type"], "tool_use")

        log = self.log_text()
        self.assertIn("[native] tool write_file", log)
        self.assertIn("tool write_file result (ok)", log)

    def test_artifacts_flow_through_the_router(self) -> None:
        client = FakeClient(
            response(tool_use("write_file", path="answer.txt", content="hi"), stop_reason="tool_use"),
            response(text_block(f"All done.\n{SUCCESS_RESULT}")),
        )
        worker = self.make_worker(client)
        config = normalize_config(
            {
                "default_worker": "echo",
                "runtime_dir": str(self.root / "runtime"),
                "timeout_seconds": 30,
            }
        )
        with Router(config, workers={"native": worker}) as router:
            ticket = router.enqueue(
                "write answer.txt", worker="native", workdir=self.workdir
            )
            ticket = router.dispatch(ticket.id)
        self.assertEqual(ticket.status.value, "done")
        self.assertEqual(ticket.artifacts, ["answer.txt"])
        self.assertEqual((self.workdir / "answer.txt").read_text(encoding="utf-8"), "hi")

    def test_result_reported_as_failed_fails_the_ticket(self) -> None:
        client = FakeClient(
            response(
                text_block(
                    'RESULT {"status": "failed", "summary": "nope", "artifacts": []}'
                )
            )
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket(), self.workdir)
        # The worker reports a clean end; the router judges the RESULT line.
        self.assertTrue(result.success)
        config = normalize_config(
            {
                "default_worker": "echo",
                "runtime_dir": str(self.root / "runtime"),
                "timeout_seconds": 30,
            }
        )
        with Router(config, workers={"native": worker}) as router:
            ticket = router.submit("do it", worker="native")
        self.assertEqual(ticket.status.value, "failed")
        self.assertIn("nope", ticket.error or "")

    # -- (b) read_file --------------------------------------------------
    def test_read_file_returns_seeded_content_to_the_model(self) -> None:
        (self.workdir / "seed.txt").write_text("seeded contents", encoding="utf-8")
        client = FakeClient(
            response(tool_use("read_file", path="seed.txt"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("read seed.txt"), self.workdir)

        self.assertTrue(result.success)
        results = self.tool_results(client.calls[1])
        self.assertFalse(results[0]["is_error"])
        self.assertIn("seeded contents", results[0]["content"])
        self.assertIn("seeded contents", self.log_text())

    def test_read_file_truncates_large_files(self) -> None:
        (self.workdir / "big.txt").write_text("x" * (MAX_READ_CHARS + 100), encoding="utf-8")
        client = FakeClient(
            response(tool_use("read_file", path="big.txt"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("read big.txt"), self.workdir)
        content = self.tool_results(client.calls[1])[0]["content"]
        self.assertIn("truncated", content)
        self.assertLess(len(content), MAX_READ_CHARS + 200)

    def test_read_file_missing_is_a_tool_error(self) -> None:
        client = FakeClient(
            response(tool_use("read_file", path="absent.txt"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("read absent.txt"), self.workdir)
        result = self.tool_results(client.calls[1])[0]
        self.assertTrue(result["is_error"])
        self.assertIn("not a file", result["content"])

    # -- (c) jail -------------------------------------------------------
    def test_write_outside_workdir_is_rejected_and_creates_nothing(self) -> None:
        outside = self.root / "evil.txt"
        client = FakeClient(
            response(
                tool_use("write_file", path="../evil.txt", content="evil"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("escape"), self.workdir)

        self.assertFalse(outside.exists())
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["content"], JAIL_ERROR)
        # The loop keeps going: the model gets the error and can still finish.
        self.assertTrue(result.success)
        self.assertIn(JAIL_ERROR, self.log_text())

    def test_absolute_path_outside_workdir_is_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        client = FakeClient(
            response(tool_use("read_file", path=str(outside)), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("read outside"), self.workdir)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["content"], JAIL_ERROR)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.workdir / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            self.skipTest("symlinks unavailable")
        client = FakeClient(
            response(tool_use("read_file", path="link/secret.txt"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("follow the link"), self.workdir)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["content"], JAIL_ERROR)

    def test_absolute_path_inside_workdir_is_allowed(self) -> None:
        (self.workdir / "inside.txt").write_text("inside", encoding="utf-8")
        client = FakeClient(
            response(
                tool_use("read_file", path=str(self.workdir / "inside.txt")),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("read inside"), self.workdir)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertFalse(tool_result["is_error"])
        self.assertIn("inside", tool_result["content"])

    # -- (d) bash -------------------------------------------------------
    def test_bash_output_is_visible_to_the_model_next_turn(self) -> None:
        client = FakeClient(
            response(tool_use("bash", command="echo native-bash-ok"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("run echo"), self.workdir)

        self.assertTrue(result.success)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertFalse(tool_result["is_error"])
        self.assertIn("native-bash-ok", tool_result["content"])
        # The model's second request carries that tool result forward.
        self.assertIn(
            "native-bash-ok",
            json.dumps(client.calls[1]["messages"], ensure_ascii=False),
        )

    def test_bash_runs_in_the_workdir_and_can_write_artifacts(self) -> None:
        client = FakeClient(
            response(
                tool_use("bash", command="printf made-by-bash > out.txt"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("write with bash"), self.workdir)
        self.assertEqual((self.workdir / "out.txt").read_text(encoding="utf-8"), "made-by-bash")

    def test_bash_failure_and_truncation_are_reported(self) -> None:
        client = FakeClient(
            response(
                tool_use("bash", command="echo boom; exit 3"),
                tool_use("bash", command="yes x | head -c 40000"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("fail and flood"), self.workdir)
        failed, flooded = self.tool_results(client.calls[1])
        self.assertTrue(failed["is_error"])
        self.assertIn("boom", failed["content"])
        self.assertIn("exit code 3", failed["content"])
        self.assertIn("truncated", flooded["content"])
        self.assertLess(len(flooded["content"]), MAX_BASH_CHARS + 200)

    def test_bash_timeout_kills_the_command(self) -> None:
        client = FakeClient(
            response(tool_use("bash", command="sleep 5"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client, bash_timeout_seconds=0.2)
        result = worker.run(self.make_ticket("sleep"), self.workdir)
        self.assertTrue(result.success)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("timed out", tool_result["content"])

    def test_bash_env_is_allow_listed(self) -> None:
        client = FakeClient(
            response(
                tool_use("bash", command="printf %s \"$NATIVE_TEST_SECRET\"; printf %s \"$NATIVE_TEST_OK\""),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(
            client,
            env={"NATIVE_TEST_OK": "visible", "NATIVE_TEST_SECRET": "hidden-value"},
            env_passthrough=["PATH", "NATIVE_TEST_OK"],
        )
        worker.run(self.make_ticket("env"), self.workdir)
        output = self.tool_results(client.calls[1])[0]["content"]
        self.assertIn("visible", output)
        self.assertNotIn("hidden-value", output)

    # -- (e) max turns --------------------------------------------------
    def test_max_turns_exhaustion_fails(self) -> None:
        client = FakeClient(
            response(tool_use("read_file", path="missing.txt"), stop_reason="tool_use")
        )
        worker = self.make_worker(client, max_turns=3)
        result = worker.run(self.make_ticket("never finish"), self.workdir)

        self.assertFalse(result.success)
        self.assertIn("max_turns", result.error or "")
        self.assertEqual(len(client.calls), 3)
        self.assertIn("max_turns", self.log_text())

    def test_final_turn_tools_are_not_executed(self) -> None:
        # On the last allowed turn there is no turn left to report results in,
        # so a write must not happen after the run is already over.
        client = FakeClient(
            response(
                tool_use("write_file", path="late.txt", content="too late"),
                stop_reason="tool_use",
            )
        )
        worker = self.make_worker(client, max_turns=1)
        result = worker.run(self.make_ticket("write on the last turn"), self.workdir)
        self.assertFalse(result.success)
        self.assertFalse((self.workdir / "late.txt").exists())

    def test_api_error_fails_the_run(self) -> None:
        from superagent_lite.planner import PlannerError

        client = FakeClient(PlannerError("planner HTTP 500: nope"))
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("boom"), self.workdir)
        self.assertFalse(result.success)
        self.assertIn("model request failed", result.error or "")
        self.assertIn("500", result.error or "")

    def test_missing_model_fails_without_calling_anything(self) -> None:
        client = FakeClient(response(text_block(SUCCESS_RESULT)))
        worker = NativeWorker(model=None, client=client)
        result = worker.run(self.make_ticket("no model"), self.workdir)
        self.assertFalse(result.success)
        self.assertIn("no model", result.error or "")
        self.assertEqual(client.calls, [])

    def test_unparsable_response_shape_fails_the_run(self) -> None:
        client = FakeClient({"role": "assistant"})
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("bad body"), self.workdir)
        self.assertFalse(result.success)
        self.assertIn("no content", result.error or "")

    # -- (f) edit_file --------------------------------------------------
    def test_edit_file_happy_path(self) -> None:
        (self.workdir / "notes.txt").write_text("alpha beta gamma", encoding="utf-8")
        client = FakeClient(
            response(
                tool_use("edit_file", path="notes.txt", old_text="beta", new_text="BETA"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("edit notes"), self.workdir)

        self.assertTrue(result.success)
        self.assertEqual(
            (self.workdir / "notes.txt").read_text(encoding="utf-8"),
            "alpha BETA gamma",
        )
        self.assertFalse(self.tool_results(client.calls[1])[0]["is_error"])

    def test_edit_file_requires_exactly_one_occurrence(self) -> None:
        (self.workdir / "dup.txt").write_text("same same", encoding="utf-8")
        (self.workdir / "clean.txt").write_text("one", encoding="utf-8")
        client = FakeClient(
            response(
                tool_use("edit_file", path="dup.txt", old_text="same", new_text="x"),
                tool_use("edit_file", path="clean.txt", old_text="absent", new_text="x"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("edit"), self.workdir)
        ambiguous, missing = self.tool_results(client.calls[1])
        self.assertTrue(ambiguous["is_error"])
        self.assertIn("2 times", ambiguous["content"])
        self.assertTrue(missing["is_error"])
        self.assertIn("not found", missing["content"])
        self.assertEqual((self.workdir / "dup.txt").read_text(encoding="utf-8"), "same same")

    # -- glob / grep ----------------------------------------------------
    def test_glob_lists_relative_paths_with_a_cap(self) -> None:
        (self.workdir / "pkg").mkdir()
        (self.workdir / "pkg" / "a.py").write_text("", encoding="utf-8")
        (self.workdir / "pkg" / "b.py").write_text("", encoding="utf-8")
        (self.workdir / "pkg" / "c.txt").write_text("", encoding="utf-8")
        for index in range(MAX_GLOB_ENTRIES + 5):
            (self.workdir / "pkg" / f"n{index:04d}.py").write_text("", encoding="utf-8")
        client = FakeClient(
            response(
                tool_use("glob", pattern="pkg/*.py"),
                tool_use("glob", pattern="**/*.txt"),
                tool_use("glob", pattern="../*"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("glob"), self.workdir)
        capped, txt, escape = self.tool_results(client.calls[1])
        self.assertFalse(capped["is_error"])
        self.assertIn("pkg/a.py", capped["content"])
        self.assertIn("truncated", capped["content"])
        self.assertNotIn("c.txt", capped["content"])
        self.assertIn("pkg/c.txt", txt["content"])
        self.assertTrue(escape["is_error"])
        self.assertEqual(escape["content"], JAIL_ERROR)

    def test_grep_finds_matches_and_skips_binary(self) -> None:
        (self.workdir / "src").mkdir()
        (self.workdir / "src" / "one.py").write_text("needle here\nother\n", encoding="utf-8")
        (self.workdir / "src" / "two.py").write_text("no match\n", encoding="utf-8")
        (self.workdir / "src" / "bin.dat").write_bytes(b"needle\0\x01\x02")
        (self.workdir / ".git").mkdir()
        (self.workdir / ".git" / "config").write_text("needle\n", encoding="utf-8")
        client = FakeClient(
            response(
                tool_use("grep", pattern="needle"),
                tool_use("grep", pattern="needle", path="src/two.py"),
                tool_use("grep", pattern="[", path="."),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("grep"), self.workdir)
        found, scoped, invalid = self.tool_results(client.calls[1])
        self.assertFalse(found["is_error"])
        self.assertIn("src/one.py:1: needle here", found["content"])
        self.assertNotIn("bin.dat", found["content"])
        self.assertNotIn(".git", found["content"])
        self.assertEqual(scoped["content"], "no matches")
        self.assertTrue(invalid["is_error"])
        self.assertIn("invalid regex", invalid["content"])

    def test_grep_caps_the_number_of_matches(self) -> None:
        (self.workdir / "many.txt").write_text("hit\n" * (MAX_GREP_MATCHES + 20), encoding="utf-8")
        client = FakeClient(
            response(tool_use("grep", pattern="hit"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        worker.run(self.make_ticket("grep cap"), self.workdir)
        content = self.tool_results(client.calls[1])[0]["content"]
        self.assertIn("truncated", content)
        self.assertEqual(
            len([line for line in content.splitlines() if line.startswith("many.txt")]),
            MAX_GREP_MATCHES,
        )

    def test_unknown_tool_is_reported_to_the_model(self) -> None:
        client = FakeClient(
            response(tool_use("delete_everything", path="/"), stop_reason="tool_use"),
            response(text_block(SUCCESS_RESULT)),
        )
        worker = self.make_worker(client)
        result = worker.run(self.make_ticket("unknown tool"), self.workdir)
        self.assertTrue(result.success)
        tool_result = self.tool_results(client.calls[1])[0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("unknown tool", tool_result["content"])

    def test_secrets_are_redacted_in_the_log(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"NATIVE_FAKE_TOKEN": "super-secret-value"}):
            client = FakeClient(
                response(
                    tool_use("bash", command="printf %s $NATIVE_FAKE_TOKEN"),
                    stop_reason="tool_use",
                ),
                response(text_block(SUCCESS_RESULT)),
            )
            worker = self.make_worker(client, env_passthrough=["PATH", "NATIVE_FAKE_TOKEN"])
            worker.run(self.make_ticket("leak check"), self.workdir)
        log = self.log_text()
        self.assertNotIn("super-secret-value", log)
        self.assertIn("***", log)


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _RecordingOpener:
    """Stands in for ``urllib.request.urlopen``: no socket is ever opened."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.requests.append(json.loads(request.data.decode("utf-8")))
        index = min(len(self.requests) - 1, len(self.bodies) - 1)
        return _FakeResponse(json.dumps(self.bodies[index]).encode("utf-8"))


class NativeMessagesClientTests(unittest.TestCase):
    """The worker over the real ``MessagesClient``, with the transport stubbed."""

    def test_client_passes_tools_and_returns_full_response(self) -> None:
        opener = _RecordingOpener(
            response(
                tool_use("write_file", path="out.txt", content="hi"),
                stop_reason="tool_use",
            ),
            response(text_block(SUCCESS_RESULT)),
        )
        client = MessagesClient(Endpoint("http://stub.invalid", "tok"), opener=opener)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            worker = NativeWorker(model="test-model", client=client)
            result = worker.run(
                Ticket(
                    id="t_client",
                    description="write out.txt",
                    worker="native",
                    workdir=str(work),
                    log_path=str(root / "t.log"),
                ),
                work,
            )
            self.assertTrue(result.success)
            self.assertEqual((work / "out.txt").read_text(encoding="utf-8"), "hi")

        first, second = opener.requests
        self.assertEqual(first["tools"], TOOLS)
        self.assertEqual(first["system"], SYSTEM_PROMPT)
        self.assertEqual(first["max_tokens"], 4096)
        tool_result = second["messages"][-1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertIn("out.txt", tool_result["content"])

    def test_lazy_client_uses_the_settings_file(self) -> None:
        opener = _RecordingOpener(response(text_block(SUCCESS_RESULT)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "http://stub.invalid",
                            "ANTHROPIC_AUTH_TOKEN": "tok",
                        }
                    }
                ),
                encoding="utf-8",
            )
            worker = NativeWorker(
                model="test-model", settings_path=str(settings), opener=opener
            )
            work = root / "work"
            work.mkdir()
            result = worker.run(
                Ticket(
                    id="t_settings",
                    description="just answer",
                    worker="native",
                    workdir=str(work),
                    log_path=str(root / "t.log"),
                ),
                work,
            )
        self.assertTrue(result.success)
        self.assertEqual(len(opener.requests), 1)

    def test_missing_endpoint_fails_the_run_not_the_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            worker = NativeWorker(
                model="test-model", settings_path=str(root / "absent.json"), env={}
            )
            result = worker.run(
                Ticket(
                    id="t_no_endpoint",
                    description="anything",
                    worker="native",
                    workdir=str(work),
                    log_path=str(root / "t.log"),
                ),
                work,
            )
        self.assertFalse(result.success)
        self.assertIn("endpoint not configured", result.error or "")


class NativeConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = default_config()
        self.assertEqual(config["native_model"], "cn:deepseek-v4.1-flash")
        self.assertEqual(config["native_max_turns"], 25)
        self.assertEqual(config["native_settings_path"], "~/.claude/settings.json")
        self.assertEqual(config["native_bash_timeout_seconds"], 120.0)

    def test_settings_path_defaults_to_the_brain_path(self) -> None:
        config = normalize_config({"brain_settings_path": "/tmp/other-settings.json"})
        self.assertEqual(config["native_settings_path"], "/tmp/other-settings.json")

    def test_explicit_settings_path_wins(self) -> None:
        config = normalize_config(
            {
                "brain_settings_path": "/tmp/other-settings.json",
                "native_settings_path": "/tmp/native.json",
            }
        )
        self.assertEqual(config["native_settings_path"], "/tmp/native.json")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            normalize_config({"native_model": ""})
        with self.assertRaises(ValueError):
            normalize_config({"native_settings_path": "  "})
        with self.assertRaises(ValueError):
            normalize_config({"native_max_turns": 0})
        with self.assertRaises(ValueError):
            normalize_config({"native_max_turns": True})
        with self.assertRaises(ValueError):
            normalize_config({"native_max_turns": "25"})
        with self.assertRaises(ValueError):
            normalize_config({"native_bash_timeout_seconds": 0})
        with self.assertRaises(ValueError):
            normalize_config({"native_bash_timeout_seconds": "soon"})

    def test_router_registers_native(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = normalize_config(
                {
                    "default_worker": "echo",
                    "runtime_dir": tmp,
                    "native_model": "some-model",
                    "native_max_turns": 7,
                    "native_bash_timeout_seconds": 9,
                }
            )
            with Router(config) as router:
                worker = router.workers["native"]
                self.assertIsInstance(worker, NativeWorker)
                self.assertEqual(worker.name, "native")
                self.assertTrue(worker.result_contract)
                self.assertEqual(worker.model, "some-model")
                self.assertEqual(worker.max_turns, 7)
                self.assertEqual(worker.bash_timeout_seconds, 9.0)


if __name__ == "__main__":
    unittest.main()
