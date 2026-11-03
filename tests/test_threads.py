#!/usr/bin/env python3
"""Tests for the per-user conversation thread store."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite.threads import ThreadStore, thread_filename  # noqa: E402


class ThreadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "threads"
        self.store = ThreadStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_returns_messages_oldest_first(self) -> None:
        self.store.append("alice", "first question", "first answer")
        self.store.append("alice", "second question", "second answer")

        history = self.store.history("alice", max_turns=10, max_chars=10_000)
        self.assertEqual(
            [message["role"] for message in history],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(history[0]["content"], "first question")
        self.assertEqual(history[1]["content"], "first answer")
        self.assertEqual(history[2]["content"], "second question")
        self.assertEqual(history[3]["content"], "second answer")

    def test_record_shape_is_a_single_json_object_per_line(self) -> None:
        self.store.append("alice", "hi", "hello")
        lines = self.store.path_for("alice").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["user"], "hi")
        self.assertEqual(record["assistant"], "hello")
        self.assertIn("ts", record)

    def test_users_do_not_share_history(self) -> None:
        self.store.append("alice", "alice says", "alice answer")
        self.store.append("bob", "bob says", "bob answer")
        self.assertEqual(
            self.store.history("bob", max_turns=10, max_chars=10_000),
            [
                {"role": "user", "content": "bob says"},
                {"role": "assistant", "content": "bob answer"},
            ],
        )

    def test_missing_file_is_an_empty_history(self) -> None:
        self.assertEqual(self.store.history("nobody", max_turns=5, max_chars=500), [])

    def test_max_turns_keeps_the_most_recent_whole_turns(self) -> None:
        for index in range(4):
            self.store.append("alice", f"q{index}", f"a{index}")
        history = self.store.history("alice", max_turns=2, max_chars=10_000)
        self.assertEqual(
            [message["content"] for message in history],
            ["q2", "a2", "q3", "a3"],
        )

    def test_max_chars_drops_oldest_turns_first(self) -> None:
        self.store.append("alice", "u" * 10, "a" * 10)  # 20 chars, the oldest
        self.store.append("alice", "u" * 10, "a" * 10)
        self.store.append("alice", "u" * 10, "a" * 10)  # newest
        history = self.store.history("alice", max_turns=10, max_chars=25)
        self.assertEqual(len(history), 2)  # only one turn fits in 25 chars
        self.assertEqual(history[0]["content"], "u" * 10)

        history = self.store.history("alice", max_turns=10, max_chars=45)
        self.assertEqual(len(history), 4)  # two turns

    def test_zero_bounds_disable_history(self) -> None:
        self.store.append("alice", "q", "a")
        self.assertEqual(self.store.history("alice", max_turns=0, max_chars=100), [])
        self.assertEqual(self.store.history("alice", max_turns=5, max_chars=0), [])

    def test_corrupt_lines_are_skipped_not_fatal(self) -> None:
        self.store.append("alice", "good question", "good answer")
        path = self.store.path_for("alice")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps({"user": "partial"}) + "\n")  # missing assistant
            handle.write(json.dumps(["a", "list"]) + "\n")
            handle.write("\n")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"user": "second q", "assistant": "second a"}) + "\n")

        history = self.store.history("alice", max_turns=10, max_chars=10_000)
        self.assertEqual(
            [message["content"] for message in history],
            ["good question", "good answer", "second q", "second a"],
        )

    def test_safe_ids_are_used_verbatim(self) -> None:
        self.assertEqual(thread_filename("alice"), "alice.jsonl")
        self.assertEqual(thread_filename("a.b-c_1"), "a.b-c_1.jsonl")

    def test_unsafe_ids_fall_back_to_a_sha1_prefix(self) -> None:
        for unsafe in ("feishu:ou_123", "../../etc/passwd", "..", "", "a/b"):
            with self.subTest(user_id=unsafe):
                name = thread_filename(unsafe)
                self.assertNotIn("/", name)
                self.assertNotIn(":", name)
                self.assertTrue(name.startswith("u_"))
                self.assertTrue(name.endswith(".jsonl"))
                self.assertNotEqual(name, "..jsonl")

        # The fallback is stable and distinct per id.
        self.assertEqual(thread_filename("feishu:x"), thread_filename("feishu:x"))
        self.assertNotEqual(thread_filename("feishu:x"), thread_filename("feishu:y"))

    def test_unsafe_id_roundtrip_stays_inside_the_root(self) -> None:
        self.store.append("feishu:ou_abc", "q", "a")
        history = self.store.history("feishu:ou_abc", max_turns=5, max_chars=100)
        self.assertEqual(history[0]["content"], "q")
        files = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, self.root)

    def test_append_never_raises_on_a_broken_root(self) -> None:
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        broken = ThreadStore(blocker / "threads")
        broken.append("alice", "q", "a")  # must not raise
        self.assertEqual(broken.history("alice", max_turns=5, max_chars=100), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
