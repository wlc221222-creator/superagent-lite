"""Ongoing-task behavior with real SQLite/queue/HTTP and scripted models.

These tests prove control flow and context delivery, not model judgement.
"""

import json
import unittest
from unittest import mock

from tests.support import wait_for
from tests import test_brain as fixtures
from tests.test_brain import BrainCase, PLAN_ONE, PLAN_TWO


def decision(action="ready", reply="Delivered and checked.", **fields):
    return json.dumps({"action": action, "reply": reply, **fields})


class TaskLifecycleTests(BrainCase):
    def finish(self, brain, task_id):
        wait_for(lambda: brain.advance_task(task_id).settled)
        return brain.tasks.get(task_id)

    def test_original_goal_and_actual_artifacts_reach_delivery_review(self):
        self.script(PLAN_ONE, decision())
        self.start_daemon()
        brain = self.brain()
        request = "Create hello.txt, preserving the existing public API."
        reply = brain.reply(request, user_id="alice")
        self.assertEqual(reply.task_status, "ready")
        task = brain.tasks.get(reply.task_id)
        self.assertEqual(task.request, request)
        self.assertIn("preserving the existing public API", self.worker.contexts[0])
        payload = json.loads(self.planner.payloads()[-1]["messages"][0]["content"])
        self.assertEqual(payload["intent"]["original_request"], request)
        self.assertIn("done: create hello.txt", json.dumps(payload["current_artifacts"]))
        self.assertEqual(brain.router.get(reply.ticket_ids[0]).task_id, task.id)

    def test_unmet_original_constraint_continues_same_task_and_directory(self):
        self.script(PLAN_ONE,
                    decision("continue", "Compatibility has not been checked.",
                             instruction="Verify that the public API remains compatible."),
                    decision())
        self.start_daemon()
        brain = self.brain()
        reply = brain.reply("Create the file without changing the public API.")
        self.assertEqual(reply.task_status, "ready", reply.text)
        self.assertEqual(len(reply.ticket_ids), 2)
        first, second = (brain.router.get(item) for item in reply.ticket_ids)
        self.assertEqual(first.workdir, second.workdir)
        self.assertEqual(first.task_id, second.task_id)
        self.assertIn("wrote hello.txt", self.worker.contexts[1])
        self.assertIn("without changing the public API", self.worker.contexts[1])
        self.assertEqual(len(self.planner.payloads()), 3)

    def test_followup_reuses_original_task_instead_of_creating_another(self):
        self.script(PLAN_ONE, decision())
        self.start_daemon()
        brain = self.brain()
        first = brain.reply("Create hello.txt.", user_id="alice")
        original = brain.tasks.get(first.task_id)
        self.script(json.dumps({"action": "followup", "task_id": first.task_id}), decision())
        # Reset the fixture counter by selecting responses relative to this request.
        start = len(self.planner.payloads())
        self.planner.responder = lambda _, i: (
            json.dumps({"action": "followup", "task_id": first.task_id})
            if i == start else decision())
        second = brain.reply("In that file, also add goodbye.", user_id="alice")
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(len(second.ticket_ids), 2)
        task = brain.tasks.get(second.task_id)
        self.assertEqual(task.request, original.request)
        self.assertEqual(task.workdir, original.workdir)
        self.assertEqual(task.updates, ["In that file, also add goodbye."])
        self.assertIn("also add goodbye", self.worker.contexts[-1])

    def test_followup_during_execution_waits_for_existing_attempt(self):
        self.script(PLAN_ONE)
        brain = self.brain()
        first = brain.reply("Create hello.txt.", user_id="alice", timeout=0)
        start = len(self.planner.payloads())
        self.planner.responder = lambda _, i: (
            json.dumps({"action": "followup", "task_id": first.task_id})
            if i == start else decision())
        second = brain.reply("Use the word goodbye instead.", user_id="alice", timeout=0)
        self.assertEqual(len(second.ticket_ids), 1)
        self.start_daemon()
        task = self.finish(brain, first.task_id)
        self.assertEqual(len(task.ticket_ids), 2)
        self.assertEqual(len(self.worker.seen), 2)

    def test_timeout_does_not_end_background_task_or_lose_final_history(self):
        self.script(PLAN_ONE, decision(reply="The actual final delivery."))
        brain = self.brain()
        reply = brain.reply("Create hello.txt.", user_id="alice", timeout=0)
        self.assertEqual(reply.mode, "pending")
        brain.start()
        self.start_daemon()
        wait_for(lambda: brain.tasks.get(reply.task_id).settled)
        task = brain.tasks.get(reply.task_id)
        self.assertEqual(task.status, "ready")
        history = brain.threads.history("alice", max_turns=10, max_chars=10000)
        self.assertEqual(history[-1]["content"], "The actual final delivery.")
        brain.advance_task(task.id)
        self.assertEqual(len(brain.threads.history("alice", max_turns=10, max_chars=10000)), 2)

    def test_restart_keeps_original_goal_and_does_not_repeat_execution(self):
        self.script(PLAN_ONE, decision())
        first_brain = self.brain()
        reply = first_brain.reply("Create hello.txt while preserving imports.", timeout=0)
        other = self.brain()
        self.start_daemon()
        task = self.finish(other, reply.task_id)
        self.assertEqual(task.status, "ready")
        self.assertEqual(len(task.ticket_ids), 1)
        self.assertEqual(len(self.worker.seen), 1)
        self.assertIn("preserving imports", task.request)

    def test_restart_adopts_a_ticket_created_before_batch_save(self):
        self.script(PLAN_ONE, decision())
        brain = self.brain()
        enqueue = brain.enqueue

        def interrupted(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise RuntimeError("interrupted between enqueue and task update")

        with mock.patch.object(brain, "enqueue", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                brain.reply("Create hello.txt.")
        task = brain.tasks.list()[0]
        self.assertEqual(task.status, "planning")
        self.assertEqual(brain.router.store.count(), 1)
        other = self.brain()
        self.start_daemon()
        task = self.finish(other, task.id)
        self.assertEqual(task.status, "ready")
        self.assertEqual(other.router.store.count(), 1)
        self.assertEqual(len(self.worker.seen), 1)

    def test_approval_holds_execution_but_not_task_identity(self):
        self.script(json.dumps({"action": "plan", "tickets": [
            {"id": "t1", "description": "git push after verification", "worker": "stub"},
        ]}), decision())
        brain = self.brain()
        reply = brain.reply("Publish the requested change.")
        self.assertEqual(reply.task_status, "awaiting_approval")
        self.assertEqual(len(self.planner.payloads()), 1)
        brain.router.approve(reply.ticket_ids[0])
        self.start_daemon()
        task = self.finish(brain, reply.task_id)
        self.assertEqual(task.status, "ready")
        self.assertEqual(task.ticket_ids, reply.ticket_ids)

    def test_background_and_foreground_do_not_duplicate_delivery_review(self):
        self.script(PLAN_ONE, decision())
        brain = self.brain()
        brain.start()
        self.start_daemon()
        reply = brain.reply("Create hello.txt.")
        self.assertEqual(reply.task_status, "ready")
        brain.advance_task(reply.task_id)
        self.assertEqual(len(self.planner.payloads()), 2)

    def test_original_conversation_is_preserved_for_worker_and_delivery(self):
        self.script(PLAN_ONE, decision())
        brain = self.brain()
        brain.threads.append("alice", "Never change the public API.", "Understood.")
        self.start_daemon()
        reply = brain.reply("Create hello.txt.", user_id="alice")
        self.assertIn("Never change the public API", self.worker.contexts[0])
        task = brain.tasks.get(reply.task_id)
        self.assertEqual(task.context[0]["content"], "Never change the public API.")

    def test_unstructured_summary_is_not_accepted_as_goal_completion(self):
        self.script(PLAN_ONE, "Everything is fine, trust me.")
        self.start_daemon()
        reply = self.brain().reply("Create hello.txt.")
        self.assertEqual(reply.task_status, "needs_input")
        self.assertIn("assessment", reply.text)

    def test_continuations_stop_at_a_visible_limit(self):
        self.script(PLAN_ONE, decision("continue", "Still missing evidence.",
                                      instruction="Obtain the missing execution evidence."))
        self.start_daemon()
        brain = self.brain(dict(self.config, brain_max_rounds=2))
        reply = brain.reply("Create hello.txt.")
        self.assertEqual(len(reply.ticket_ids), 2)
        self.assertEqual(reply.task_status, "needs_input")
        self.assertIn("not complete", reply.text)

    def test_human_question_does_not_spawn_automatic_work(self):
        self.script(PLAN_ONE, decision("needs_input", "Which of the two interfaces must be retained?"))
        self.start_daemon()
        brain = self.brain()
        reply = brain.reply("Adjust the interface.")
        for _ in range(3):
            brain.advance_task(reply.task_id)
        self.assertEqual(reply.task_status, "needs_input")
        self.assertEqual(len(self.worker.seen), 1)

    def test_dependency_results_are_handed_over_as_context(self):
        self.script(PLAN_TWO, decision())
        self.start_daemon()
        self.brain().reply("Do these two related things.")
        self.assertIn("Earlier execution results", self.worker.contexts[1])
        self.assertIn("wrote hello.txt", self.worker.contexts[1])

    def test_status_route_does_not_enqueue_work(self):
        self.script(PLAN_ONE)
        brain = self.brain()
        first = brain.reply("Create hello.txt.", user_id="alice", timeout=0)
        self.planner.responder = lambda _, i: json.dumps({"action": "status", "task_id": first.task_id})
        reply = brain.reply("How is that task going?", user_id="alice")
        self.assertEqual(reply.task_id, first.task_id)
        self.assertEqual(brain.router.store.count(), 1)

    def test_followup_cannot_attach_to_another_users_task(self):
        self.script(PLAN_ONE)
        brain = self.brain()
        first = brain.reply("Create hello.txt.", user_id="alice", timeout=0)
        self.planner.responder = lambda _, i: json.dumps({"action": "followup", "task_id": first.task_id})
        reply = brain.reply("Change that file.", user_id="bob", timeout=0)
        self.assertEqual(reply.mode, "error")
        self.assertEqual(brain.tasks.get(first.task_id).updates, [])


class TaskHttpTests(BrainCase):
    serve = fixtures.HttpApiTests.serve
    request = fixtures.HttpApiTests.request

    def setUp(self):
        super().setUp()
        self.token = "task-test-token"
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        super().tearDown()

    def test_task_endpoint_returns_final_delivery_after_chat_times_out(self):
        self.script(PLAN_ONE, decision())
        brain = self.brain(dict(self.config, chat_timeout_seconds=0.05))
        base, _ = self.serve(token=self.token, brain=brain)
        status, body = self.request(base + "/chat", payload={"text": "Create hello.txt.", "user_id": "alice"}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "pending")
        self.start_daemon()
        wait_for(lambda: brain.tasks.get(body["task_id"]).settled)
        status, result = self.request(base + "/tasks/" + body["task_id"], token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(result["task_status"], "ready")
        self.assertEqual(result["reply"], "Delivered and checked.")
        status, result = self.request(base + "/tasks?user_id=alice", token=self.token)
        self.assertEqual(len(result["tasks"]), 1)

    def test_tasks_require_same_auth_as_tickets(self):
        base, _ = self.serve(token=self.token)
        status, _ = self.request(base + "/tasks/unknown")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
