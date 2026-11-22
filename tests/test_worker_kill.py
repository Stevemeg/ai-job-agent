"""A worker process killed mid-task: the task is recovered via lease expiry
and completes exactly once. Real subprocesses, real kill, real Postgres."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.database.db import init_db           # noqa: E402

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "_worker_harness.py"
LEASE_S = 2.0


def _spawn(pg, log_path, *args):
    with pg.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    env = dict(os.environ, PGOPTIONS=f"-csearch_path={schema}",
               PYTHONUNBUFFERED="1")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HARNESS), *args], cwd=ROOT,
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    proc._log = log                                   # closed by _finish()
    return proc


def _finish(proc, timeout):
    try:
        return proc.wait(timeout=timeout)
    finally:
        proc._log.close()


def _wait_for(path: Path, timeout: float, proc=None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass                                  # mid-write; retry
        if proc is not None and proc.poll() is not None:
            break
        time.sleep(0.05)
    raise AssertionError(f"{path.name} never appeared")


def _row(pg, task_id):
    with pg.connect() as conn:
        return conn.execute(text("SELECT * FROM tasks WHERE id = :i"),
                            {"i": task_id}).mappings().one()


def test_killed_worker_task_completes_exactly_once_on_another_worker(pg, tmp_path):
    init_db()
    markers = tmp_path / "markers"
    markers.mkdir()
    task_id = taskqueue.enqueue("slow", {"marker_dir": str(markers),
                                         "first_attempt_s": 120, "retry_s": 0.5})

    # Worker A claims the task and is SIGKILLed mid-handler.
    proc_a = _spawn(pg, tmp_path / "worker_a.log", "--lease", str(LEASE_S), "--once")
    started_a = _wait_for(markers / "started-1.json", 60, proc_a)
    worker_a = started_a["worker_id"]
    time.sleep(LEASE_S)               # A is alive and heartbeating meanwhile
    assert _row(pg, task_id)["lease_expires_at"] is not None
    proc_a.kill()                     # SIGKILL on POSIX, TerminateProcess on Windows
    code_a = _finish(proc_a, 30)
    if os.name == "posix":
        assert code_a == -signal.SIGKILL
    killed = _row(pg, task_id)
    print(f"\n[kill] worker A {worker_a} killed (exit {code_a}) mid-task: "
          f"status={killed['status']} attempts={killed['attempts']} "
          f"locked_by={killed['locked_by']}")
    assert (killed["status"], killed["attempts"], killed["locked_by"]) == \
        ("running", 1, worker_a)

    # Worker B finds it once A's lease has lapsed, and runs attempt 2.
    proc_b = _spawn(pg, tmp_path / "worker_b.log", "--lease", str(LEASE_S), "--once")
    code_b = _finish(proc_b, 90)
    out_b = (tmp_path / "worker_b.log").read_text(encoding="utf-8")
    assert code_b == 0, out_b
    worker_b = json.loads((markers / "started-2.json").read_text())["worker_id"]
    done = _row(pg, task_id)
    print(f"[kill] worker B {worker_b} (exit {code_b}): status={done['status']} "
          f"attempts={done['attempts']} result={done['result']} "
          f"last_error={done['last_error']!r}")

    assert worker_b != worker_a
    assert done["status"] == "succeeded"
    assert done["attempts"] == 2
    assert done["locked_by"] == worker_b
    assert done["result"] == {"by": worker_b, "attempt": 2}
    assert f"lease expired (worker {worker_a}, attempt 1/3)" in done["last_error"]
    completions = (markers / "completions.log").read_text().split()
    assert completions == [worker_b]          # the handler finished exactly once

    # The dead worker's id can no longer touch the task.
    assert taskqueue.complete(task_id, worker_a, {"by": worker_a}) is False
    assert taskqueue.fail(task_id, worker_a, "late failure") is False
    assert taskqueue.heartbeat(task_id, worker_a, LEASE_S) is False
    after = _row(pg, task_id)
    assert (after["status"], after["result"], after["attempts"]) == \
        ("succeeded", {"by": worker_b, "attempt": 2}, 2)
    print(f"[kill] complete()/fail()/heartbeat() from {worker_a} rejected; "
          "task unchanged")


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM delivery needs POSIX")
def test_sigterm_finishes_the_current_task_then_exits_cleanly(pg, tmp_path):
    init_db()
    markers = tmp_path / "markers"
    markers.mkdir()
    task_id = taskqueue.enqueue("slow", {"marker_dir": str(markers),
                                         "first_attempt_s": 2.0, "retry_s": 0})
    proc = _spawn(pg, tmp_path / "worker.log", "--lease", "5", "--grace", "20",
                  "--forever")
    _wait_for(markers / "started-1.json", 60, proc)
    later = taskqueue.enqueue("slow", {"marker_dir": str(markers),
                                       "first_attempt_s": 0, "retry_s": 0})
    proc.send_signal(signal.SIGTERM)
    code = _finish(proc, 30)
    log = (tmp_path / "worker.log").read_text(encoding="utf-8")
    assert code == 0, log
    assert _row(pg, task_id)["status"] == "succeeded"     # finished in grace
    assert _row(pg, later)["status"] == "queued"          # never claimed
