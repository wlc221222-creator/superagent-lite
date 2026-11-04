#!/usr/bin/env python3
"""Streaming chat: Brain ``on_event`` and ``POST /chat/stream``.

The fixtures are the ones from :mod:`tests.test_brain`: a local stub planner,
a scripted worker and a real :class:`~superagent_lite.daemon.Daemon`.  No test
here touches the network or a model.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent_lite.brain import MODE_REPLY, MODE_SUMMARY  # noqa: E402
from superagent_lite.http_api import ApiConfig, make_server  # noqa: E402
from tests.test_brain import (  # noqa: E402
    PLAN_ONE,
    PLAN_TWO,
    BrainCase,
)


def parse_sse(raw: bytes) -> list[str]:
    """Return the ``data:`` payloads (comments and blank lines dropped)."""

    payloads: list[str] = []
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data: "):
            payloads.append(line[len("data: "):])
    return payloads


def parse_events(raw: bytes) -> list[dict]:
    return [json.loads(payload) for payload in parse_sse(raw)]


class BrainEventTests(BrainCase):
    def test_plan_emits_planning_tickets_updates_then_done(self) -> None:
        self.script(PLAN_TWO, "SUMMARY: both steps finished.")
        self.start_daemon()
        events: list[dict] = []

        reply = self.brain().reply("do two things", on_event=events.append)

        self.assertEqual(reply.mode, MODE_SUMMARY)
        self.assertEqual(events[0], {"type": "planning"})
        self.assertEqual(events[1]["type"], "tickets")
        self.assertEqual(events[1]["ticket_ids"], reply.ticket_ids)

        updates = [event for event in events[2:-1] if event["type"] == "ticket"]
        # Every ticket is created, reports at least one change, and ends done.
        for ticket_id in reply.ticket_ids:
            statuses = [
                event["status"] for event in updates if event["id"] == ticket_id
            ]
            self.assertTrue(statuses, f"no update for {ticket_id}")
            self.assertEqual(statuses[-1], "done")

        last = events[-1]
        self.assertEqual(last["type"], "done")
        self.assertEqual(last["reply"], reply.text)
        self.assertEqual(last["mode"], reply.mode)
        self.assertEqual(last["ticket_ids"], reply.ticket_ids)
        self.assertEqual(last["timed_out"], reply.timed_out)
        self.assertEqual(last["model"], reply.model)
        self.assertEqual(set(last), {"type", "reply", "mode", "ticket_ids",
                                     "timed_out", "model"})

    def test_ticket_updates_are_emitted_only_on_change(self) -> None:
        self.script(PLAN_ONE)
        # No daemon: the ticket stays pending for the short wait.
        events: list[dict] = []
        reply = self.brain().reply("create hello.txt", timeout=0.15,
                                   on_event=events.append)
        self.assertEqual(reply.mode, "pending")
        updates = [event for event in events if event["type"] == "ticket"]
        # The first poll sees the state the tickets were enqueued in, so a
        # ticket that never changes produces no event at all.
        self.assertEqual(updates, [])

    def test_reply_mode_emits_planning_then_done_without_tickets(self) -> None:
        self.script('{"action": "reply", "reply": "hello"}')
        events: list[dict] = []
        reply = self.brain().reply("hi", on_event=events.append)

        self.assertEqual(reply.mode, MODE_REPLY)
        self.assertEqual([event["type"] for event in events], ["planning", "done"])
        self.assertEqual(events[1]["reply"], "hello")
        self.assertEqual(events[1]["ticket_ids"], [])

    def test_a_broken_callback_never_breaks_the_chat(self) -> None:
        self.script(PLAN_ONE, "SUMMARY: created.")
        self.start_daemon()

        def explode(_event: dict) -> None:
            raise RuntimeError("client went away")

        reply = self.brain().reply("create hello.txt", on_event=explode)
        self.assertEqual(reply.mode, MODE_SUMMARY)
        self.assertEqual(reply.text, "SUMMARY: created.")

    def test_without_a_callback_nothing_changes(self) -> None:
        self.script('{"action": "reply", "reply": "plain"}')
        reply = self.brain().reply("hi")
        self.assertEqual(reply.to_dict(), {
            "reply": "plain", "mode": "reply", "ticket_ids": [],
            "timed_out": False, "model": "stub-planner-model",
        })


class StreamEndpointTests(BrainCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = "stream-token"
        self._servers: list = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        super().tearDown()

    def serve(self, token: str | None = None, brain=None):
        server = make_server(
            brain or self.brain(),
            ApiConfig(host="127.0.0.1", port=0, token=token),
        )
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self._servers.append(server)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def stream(self, url: str, payload=None, token: str | None = None):
        """POST and return ``(status, headers, raw_body)``."""

        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url, data=data, headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_trivial_chat_streams_accepted_planning_done(self) -> None:
        self.script('{"action": "reply", "reply": "streamed hello"}')
        base = self.serve(token=None)
        status, headers, body = self.stream(base + "/chat/stream", {"text": "hi"})

        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = parse_events(body)
        self.assertEqual(
            [event["type"] for event in events], ["accepted", "planning", "done"]
        )
        self.assertEqual(events[-1]["reply"], "streamed hello")
        self.assertEqual(events[-1]["mode"], "reply")
        # Every event is one `data: {json}` line followed by a blank line.
        self.assertIn(b"data: {\"type\": \"accepted\"}\n\n", body)

    def test_plan_stream_carries_ticket_events_and_summary(self) -> None:
        self.script(PLAN_ONE, "SUMMARY: hello.txt created.")
        self.start_daemon()
        base = self.serve(token=None)
        status, headers, body = self.stream(
            base + "/chat/stream", {"text": "create hello.txt"}
        )

        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = parse_events(body)
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "accepted")
        self.assertEqual(types[1], "planning")
        self.assertEqual(types[2], "tickets")
        self.assertEqual(types[-1], "done")
        ticket_id = events[2]["ticket_ids"][0]
        statuses = [event["status"] for event in events if event["type"] == "ticket"]
        self.assertEqual(statuses[-1], "done")
        self.assertTrue(all(
            event["id"] == ticket_id for event in events if event["type"] == "ticket"
        ))
        self.assertEqual(events[-1]["mode"], "summary")
        self.assertEqual(events[-1]["ticket_ids"], [ticket_id])

    def test_error_reply_is_an_error_event_not_a_bad_gateway(self) -> None:
        self.planner.status = 500
        base = self.serve(token=None)
        status, headers, body = self.stream(base + "/chat/stream", {"text": "hi"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = parse_events(body)
        self.assertEqual(events[0]["type"], "accepted")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["mode"], "error")
        self.assertIn("planner", events[-1]["reply"])

    def test_unauthorized_stream_is_401_without_a_stream(self) -> None:
        self.script('{"action": "reply", "reply": "should not happen"}')
        base = self.serve(token=self.token)
        status, headers, body = self.stream(base + "/chat/stream", {"text": "hi"})
        self.assertEqual(status, 401)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body.decode())["error"], "unauthorized")
        self.assertEqual(self.planner.payloads(), [])

    def test_bad_stream_request_is_a_400(self) -> None:
        base = self.serve(token=None)
        status, headers, body = self.stream(base + "/chat/stream", {"text": ""})
        self.assertEqual(status, 400)
        self.assertIn("application/json", headers["Content-Type"])

    def test_idle_stream_sends_heartbeat_comments(self) -> None:
        release = threading.Event()
        seen: list[str] = []

        def slow_responder(_payload, _index):
            release.wait(10)
            return '{"action": "reply", "reply": "after the wait"}'

        self.planner.responder = slow_responder
        base = self.serve(token=None)
        data = json.dumps({"text": "hi"}).encode()
        request = urllib.request.Request(
            base + "/chat/stream", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with mock.patch(
            "superagent_lite.http_api.STREAM_HEARTBEAT_SECONDS", 0.05
        ):
            with urllib.request.urlopen(request, timeout=20) as response:
                self.assertEqual(response.status, 200)
                while True:
                    line = response.readline()
                    if not line:
                        break
                    seen.append(line.decode("utf-8").rstrip())
                    if ": ping" in seen:
                        release.set()
                    if any(entry.startswith('data: {"type": "done"') for entry in seen):
                        break
        self.assertIn(": ping", seen)
        self.assertTrue(any(entry == 'data: {"type": "accepted"}' for entry in seen))

    def test_plain_chat_still_works_alongside_the_stream(self) -> None:
        self.script('{"action": "reply", "reply": "still fine"}')
        base = self.serve(token=None)
        self.stream(base + "/chat/stream", {"text": "hi"})
        status, headers, body = self.stream(base + "/chat", {"text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode())["reply"], "still fine")


class WebUiStreamingTests(unittest.TestCase):
    def test_page_streams_and_keeps_the_plain_chat_fallback(self) -> None:
        from superagent_lite.webui import CHAT_PAGE

        self.assertIn('"/chat/stream"', CHAT_PAGE)
        self.assertIn('"/chat"', CHAT_PAGE)  # fallback path stays
        self.assertIn("getReader", CHAT_PAGE)
        self.assertIn("正在规划", CHAT_PAGE)
        self.assertIn("已派单", CHAT_PAGE)
        self.assertIn('className = "status"', CHAT_PAGE)
        # The status line uses the 12px muted design token.
        self.assertIn(".status {", CHAT_PAGE)
        self.assertIn("font-size: 12px", CHAT_PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
