"""Manual browser QA fixture: python -m tests.task_ui_fixture.

Uses temporary data and scripted model responses, never real credentials.
"""

import json
import os
import signal
import time

from superagent_lite.http_api import ApiConfig, make_server
from tests.test_brain import BrainCase, PLAN_ONE


def main():
    case = BrainCase()
    case.setUp()
    case.config["chat_timeout_seconds"] = 0.1
    brain = case.brain()
    original_run = case.worker.run

    def delayed(ticket, workdir):
        time.sleep(5)
        return original_run(ticket, workdir)

    case.worker.run = delayed

    def respond(payload, index):
        if "delivery coordinator" in payload["system"]:
            return json.dumps({"action": "ready", "reply": "已交付：hello.txt。此为本地模拟任务，不是真实模型评测。"})
        tasks = brain.tasks.list(user_id="web")
        if tasks:
            return json.dumps({"action": "followup", "task_id": tasks[0].id})
        return PLAN_ONE

    case.planner.responder = respond
    case.start_daemon()
    server = make_server(brain, ApiConfig(host="127.0.0.1", port=0))

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print(f"TASK_UI_FIXTURE pid={os.getpid()} url=http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        case.tearDown()


if __name__ == "__main__":
    main()
