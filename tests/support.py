"""Test helpers: a stub ``claude`` executable and small fixture builders."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

STUB_SCRIPT = r'''#!/usr/bin/env python3
"""Stub Claude Code CLI used by the tests.

Behaviour comes from stub_state.json next to this script; the argv and the
(child) environment it was given are recorded in stub_record.json.
"""

import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
state_path = here / "stub_state.json"
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

record = {
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": dict(os.environ),
}
(here / "stub_record.json").write_text(json.dumps(record), encoding="utf-8")


def track(event):
    """Append one line to the shared tracker file, if the test asked for it."""
    target = state.get("track")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(event + "\n")
        handle.flush()


extra = state.get("stdout")
if extra:
    print(extra)

mode = state.get("mode", "success")
if mode == "sleep":
    track("start " + os.getcwd())
    time.sleep(float(state.get("sleep", 1.0)))
    track("end " + os.getcwd())
    Path("answer.txt").write_text(state.get("content", "chain-ok"), encoding="utf-8")
    print("Wrote answer.txt into the working directory.")
    print('RESULT {"status": "success", "summary": "created answer.txt", '
          '"artifacts": ["answer.txt"]}')
elif mode == "success":
    Path("answer.txt").write_text(state.get("content", "chain-ok"), encoding="utf-8")
    print("Wrote answer.txt into the working directory.")
    print('RESULT {"status": "success", "summary": "created answer.txt", '
          '"artifacts": ["answer.txt"]}')
elif mode == "failed":
    print('RESULT {"status": "failed", "summary": "cannot write outside the '
          'working directory", "artifacts": []}')
elif mode == "missing":
    print("I am sorry, but I need you to grant me permission first.")
elif mode == "garbage":
    print("RESULT {this is not json}")
elif mode == "wrong_status":
    print('RESULT {"status": "ok", "summary": "fine", "artifacts": []}')
elif mode == "nonzero":
    print("fatal: something broke", file=sys.stderr)
    sys.exit(3)
elif mode == "escape":
    target = state.get("target", "../escaped.txt")
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text("escaped", encoding="utf-8")
    except OSError as exc:
        print('RESULT {"status": "failed", "summary": "cannot write '
              + target + ': ' + str(exc) + '", "artifacts": []}')
        sys.exit(0)
    print('RESULT {"status": "success", "summary": "wrote outside", '
          '"artifacts": ["' + target + '"]}')
else:
    raise SystemExit("unknown stub mode: " + mode)

sys.exit(int(state.get("exit_code", 0)))
'''

STUB_NAME = "claude"


def make_stub_claude(
    directory: str | Path,
    mode: str = "success",
    name: str = STUB_NAME,
    **state,
) -> Path:
    """Create an executable stub ``claude`` in ``directory``.

    ``mode`` and extra keyword arguments (``content``, ``stdout``,
    ``exit_code``, ``target``) are written to ``stub_state.json`` next to it.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(STUB_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    set_stub_state(directory, mode=mode, **state)
    return script


def set_stub_state(directory: str | Path, **state) -> None:
    path = Path(directory) / "stub_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")


def stub_record(directory: str | Path) -> dict:
    path = Path(directory) / "stub_record.json"
    return json.loads(path.read_text(encoding="utf-8"))


def python_executable() -> str:
    return sys.executable or "python3"


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02):
    """Poll ``predicate`` until it returns a truthy value or ``timeout``."""

    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        _time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout:g}s: {predicate}")
