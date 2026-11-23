"""The in-process Worker (backend.worker) against a real Postgres: handler
failures, heartbeats, lease loss, concurrent workers, graceful stop.
The cross-process SIGKILL test is tests/test_worker_kill.py."""
import threading
import time

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.database.db import init_db           # noqa: E402
from backend.worker import EXIT_GRACE_EXPIRED, EXIT_OK, Worker   # noqa: E402

pytestmark = pytest.mark.db


def _row(pg, task_id):
    with pg.connect() as conn:
        return conn.execute(text("SELECT *, now() AS db_now FROM tasks "
                                 "WHERE id = :i"), {"i": task_id}).mappings().one()


def _count(pg):
    with pg.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM tasks")).scalar()


def _make_due(pg, task_id):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() WHERE id = :i"),
                     {"i": task_id})


def _expire_lease(pg, task_id):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET lease_expires_at = now() - "
                          "interval '1 second' WHERE id = :i"), {"i": task_id})


def test_failing_handler_requeues_with_backoff_then_dies(pg):
    task_id = taskqueue.enqueue("boom", {}, max_attempts=3)

    def boom(payload, ctx):
        raise ValueError(f"kaput on attempt {ctx.task.attempts}")

    worker = Worker({"boom": boom}, lease_s=30)
    base = taskqueue.BACKOFF_BASE_S
    for attempt in (1, 2):
        result = worker.run_once()
        assert (result.outcome, result.attempt) == ("failed", attempt)
        row = _row(pg, task_id)
        assert (row["status"], row["attempts"]) == ("queued", attempt)
        assert row["locked_by"] is None and row["lease_expires_at"] is None
        assert row["last_error"].startswith(f"ValueError: kaput on attempt {attempt}")
        assert "Traceback" in row["last_error"]
        delay = (row["run_after"] - row["db_now"]).total_seconds()
        max_delay = base * 2 ** (attempt - 1)                 # before jitter
        assert max_delay / 2 - 1 <= delay <= max_delay, delay  # future, jittered
        assert worker.run_once() is None                       # backing off
        _make_due(pg, task_id)

    result = worker.run_once()
    assert (result.outcome, result.attempt) == ("failed", 3)
    row = _row(pg, task_id)
    assert (row["status"], row["attempts"]) == ("dead", 3)
    assert row["last_error"].startswith("ValueError: kaput on attempt 3")
    assert row["finished_at"] is not None
    _make_due(pg, task_id)
    assert worker.run_once() is None                           # dead stays dead


def test_unserializable_handler_result_fails_the_attempt(pg):
    task_id = taskqueue.enqueue("odd", {})
    worker = Worker({"odd": lambda payload, ctx: {"when": object()}}, lease_s=30)
    assert worker.run_once().outcome == "failed"
    assert _row(pg, task_id)["last_error"].startswith("TypeError")


def test_the_dead_handler_runs_once_when_a_task_dies(pg):
    died = []
    failing = taskqueue.enqueue("boom", {"n": 1}, max_attempts=2)

    def boom(payload, ctx):
        raise ValueError("kaput")

    def broken_dead_handler(payload, error):
        raise RuntimeError("a dead handler error is logged, not raised")

    worker = Worker({"boom": boom, "slow": lambda p, c: None}, lease_s=30,
                    dead_handlers={"boom": lambda p, e: died.append((p, e)),
                                   "slow": broken_dead_handler,
                                   "not_mine": lambda p, e: None})
    assert set(worker.dead_handlers) == {"boom", "slow"}
    assert worker.run_once().outcome == "failed"
    assert died == []                                    # attempts left
    _make_due(pg, failing)
    assert worker.run_once().outcome == "failed"         # the last attempt
    assert [(p, e.split("\n", 1)[0]) for p, e in died] == [({"n": 1},
                                                           "ValueError: kaput")]

    # Dead through lease expiry on the final attempt: claim() marks it.
    stuck = taskqueue.enqueue("slow", {"n": 2}, max_attempts=1)
    assert taskqueue.claim("gone", ["slow"], 30).id == stuck
    _expire_lease(pg, stuck)
    assert worker.run_once() is None                     # handler raised: logged
    assert _row(pg, stuck)["status"] == "dead"
    stuck2 = taskqueue.enqueue("boom", {"n": 3}, max_attempts=1)
    assert taskqueue.claim("gone", ["boom"], 30).id == stuck2
    _expire_lease(pg, stuck2)
    assert worker.run_once() is None
    assert died[-1] == ({"n": 3}, "lease expired (worker gone, attempt 1/1)")


# ---- concurrency ------------------------------------------------------------------

def test_two_workers_claiming_20_tasks_run_each_exactly_once(pg):
    init_db()
    ids = [taskqueue.enqueue("count", {"i": i}) for i in range(20)]
    runs, lock = [], threading.Lock()

    def handler(payload, ctx):
        time.sleep(0.02)
        with lock:
            runs.append((ctx.task.id, ctx.worker_id))
        return {"i": payload["i"]}

    workers = [Worker({"count": handler}, lease_s=30) for _ in range(2)]
    barrier = threading.Barrier(len(workers))
    errors = []

    def drain(worker):
        try:
            barrier.wait()
            while worker.run_once() is not None:
                pass
        except Exception as exc:                  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    assert sorted(task_id for task_id, _ in runs) == sorted(ids)   # each exactly once
    assert {worker_id for _, worker_id in runs} == {w.worker_id for w in workers}
    with pg.connect() as conn:
        rows = conn.execute(text("SELECT status, attempts FROM tasks")).all()
    assert rows and all(r == ("succeeded", 1) for r in rows)


# ---- heartbeat thread + lease loss ------------------------------------------------

def test_heartbeat_keeps_a_slow_task_leased(pg):
    task_id = taskqueue.enqueue("slow", {})

    def slow(payload, ctx):
        time.sleep(2.5)                            # > lease, < 3 heartbeats' worth
        return {"done": True}

    worker = Worker({"slow": slow}, lease_s=1.5)
    holder = threading.Thread(target=worker.run_once)
    holder.start()
    time.sleep(2.0)                                # past the original lease
    assert taskqueue.claim("w2", ["slow"], 30) is None
    holder.join(10)
    row = _row(pg, task_id)
    assert (row["status"], row["attempts"]) == ("succeeded", 1)


def test_handler_sees_lease_loss_and_its_result_is_rejected(pg):
    task_id = taskqueue.enqueue("slow", {})
    reclaimed = threading.Event()
    seen = {}

    def slow(payload, ctx):
        reclaimed.wait(10)
        deadline = time.monotonic() + 5
        while not ctx.lease_lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["lost"] = ctx.lease_lost.is_set()
        ctx.ensure_lease()                          # raises LeaseLost
        return {"should": "never be stored"}

    worker = Worker({"slow": slow}, lease_s=0.6)
    out = {}
    holder = threading.Thread(target=lambda: out.update(r=worker.run_once()))
    holder.start()
    time.sleep(0.3)
    stolen = None
    for _ in range(20):            # a heartbeat may land between the two steps
        _expire_lease(pg, task_id)
        stolen = taskqueue.claim("w2", ["slow"], 30)
        if stolen is not None:
            break
    assert stolen is not None and stolen.id == task_id
    reclaimed.set()
    holder.join(15)
    assert seen["lost"] is True
    assert out["r"].outcome == "fenced"
    assert "LeaseLost" in out["r"].error
    row = _row(pg, task_id)
    assert (row["status"], row["locked_by"], row["result"]) == ("running", "w2", None)


# ---- graceful stop --------------------------------------------------------------------

def test_stop_request_lets_the_current_task_finish_and_claims_nothing_more(pg):
    first = taskqueue.enqueue("gate", {})
    second = taskqueue.enqueue("gate", {})
    started, release = threading.Event(), threading.Event()

    def gate(payload, ctx):
        started.set()
        release.wait(10)
        return {"ok": True}

    worker = Worker({"gate": gate}, lease_s=30, grace_s=10, poll_min_s=0.05)
    out = {}
    runner = threading.Thread(target=lambda: out.update(code=worker.run_forever()))
    runner.start()
    assert started.wait(10)
    worker.request_stop()
    time.sleep(0.3)
    assert runner.is_alive()                        # waiting for the task
    release.set()
    runner.join(10)
    assert out["code"] == EXIT_OK
    assert _row(pg, first)["status"] == "succeeded"
    assert _row(pg, second)["status"] == "queued"


def test_grace_period_expiry_abandons_the_task_to_lease_recovery(pg):
    task_id = taskqueue.enqueue("stuck", {})
    started, release = threading.Event(), threading.Event()

    def stuck(payload, ctx):
        started.set()
        release.wait(30)
        raise RuntimeError("abandoned")

    worker = Worker({"stuck": stuck}, lease_s=30, grace_s=0.5, poll_min_s=0.05)
    out = {}
    runner = threading.Thread(target=lambda: out.update(code=worker.run_forever()))
    runner.start()
    assert started.wait(10)
    worker.request_stop()
    runner.join(10)
    assert out["code"] == EXIT_GRACE_EXPIRED
    row = _row(pg, task_id)
    assert (row["status"], row["locked_by"]) == ("running", worker.worker_id)
    release.set()                                   # let the loop thread unwind
    worker._loop_thread.join(10)
