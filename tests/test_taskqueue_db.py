"""backend.taskqueue against a real Postgres (throwaway schema per test,
see conftest.py)."""
from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.database.db import init_db           # noqa: E402

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


# ---- enqueue -------------------------------------------------------------------

def test_same_idempotency_key_twice_is_one_row_same_id(pg):
    first = taskqueue.enqueue("rank", {"n": 1}, idempotency_key="rank:2026-11-22")
    second = taskqueue.enqueue("rank", {"n": 2}, idempotency_key="rank:2026-11-22")
    assert first == second
    assert _count(pg) == 1
    assert _row(pg, first)["payload"] == {"n": 1}         # first enqueue wins


def test_idempotency_key_of_a_finished_task_still_returns_it(pg):
    task_id = taskqueue.enqueue("rank", {}, idempotency_key="once")
    task = taskqueue.claim("w1", ["rank"], 30)
    assert taskqueue.complete(task.id, "w1", {"ok": True})
    assert taskqueue.enqueue("rank", {}, idempotency_key="once") == task_id
    assert taskqueue.claim("w1", ["rank"], 30) is None   # never runs twice


def test_enqueue_inside_a_rolled_back_transaction_leaves_no_task(pg):
    init_db()
    with pg.connect() as conn:
        trans = conn.begin()
        task_id = taskqueue.enqueue("rank", {}, conn=conn)
        assert task_id is not None
        trans.rollback()
    assert _count(pg) == 0


def test_enqueue_joins_the_callers_transaction(pg):
    init_db()
    with pg.connect() as conn:
        trans = conn.begin()
        task_id = taskqueue.enqueue("rank", {}, conn=conn)
        assert _count(pg) == 0                     # not visible before commit
        trans.commit()
    assert _row(pg, task_id)["status"] == "queued"


def test_concurrency_key_conflict_raises_and_keeps_callers_txn_usable(pg):
    init_db()
    taskqueue.enqueue("rank", {}, concurrency_key="rank:default")
    with pytest.raises(taskqueue.ConcurrencyConflict):
        taskqueue.enqueue("rank", {}, concurrency_key="rank:default")

    with pg.connect() as conn:
        trans = conn.begin()
        with pytest.raises(taskqueue.ConcurrencyConflict):
            taskqueue.enqueue("rank", {}, concurrency_key="rank:default", conn=conn)
        other = taskqueue.enqueue("scan", {}, conn=conn)    # savepoint rolled back only
        trans.commit()
    assert _row(pg, other)["kind"] == "scan"
    assert _count(pg) == 2


# ---- claim + fencing --------------------------------------------------------------

def test_claim_increments_attempts_and_sets_the_lease(pg):
    task_id = taskqueue.enqueue("rank", {"x": 1}, max_attempts=5)
    task = taskqueue.claim("w1", ["rank", "other"], 30)
    assert (task.id, task.kind, task.payload) == (task_id, "rank", {"x": 1})
    assert (task.attempts, task.max_attempts, task.worker_id) == (1, 5, "w1")
    row = _row(pg, task_id)
    assert (row["status"], row["locked_by"], row["attempts"]) == ("running", "w1", 1)
    assert row["started_at"] is not None and row["finished_at"] is None
    assert timedelta(seconds=25) < row["lease_expires_at"] - row["db_now"] \
        <= timedelta(seconds=30)
    assert taskqueue.claim("w2", ["rank"], 30) is None        # leased


def test_claim_respects_kinds_and_run_after(pg):
    from datetime import datetime, timezone
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    later = taskqueue.enqueue("rank", {}, run_after=future)
    scan = taskqueue.enqueue("scan", {})
    assert taskqueue.claim("w1", ["rank"], 30) is None
    assert taskqueue.claim("w1", ["scan"], 30).id == scan
    _make_due(pg, later)
    assert taskqueue.claim("w1", ["rank"], 30).id == later


def test_only_the_lease_holder_can_heartbeat_complete_or_fail(pg):
    task_id = taskqueue.enqueue("rank", {})
    taskqueue.claim("w1", ["rank"], 30)
    assert taskqueue.heartbeat(task_id, "intruder", 30) is False
    assert taskqueue.complete(task_id, "intruder", {}) is False
    assert taskqueue.fail(task_id, "intruder", "nope") is False
    assert _row(pg, task_id)["status"] == "running"

    assert taskqueue.heartbeat(task_id, "w1", 30) is True
    assert taskqueue.complete(task_id, "w1", {"ranked_count": 3}) is True
    row = _row(pg, task_id)
    assert (row["status"], row["result"]) == ("succeeded", {"ranked_count": 3})
    assert row["finished_at"] is not None and row["lease_expires_at"] is None
    # finished: even the former holder is fenced out now
    assert taskqueue.complete(task_id, "w1", {"again": 1}) is False
    assert taskqueue.heartbeat(task_id, "w1", 30) is False


def test_expired_lease_is_reclaimed_and_the_old_holder_is_fenced(pg):
    task_id = taskqueue.enqueue("rank", {})
    taskqueue.claim("w1", ["rank"], 30)
    _expire_lease(pg, task_id)
    task = taskqueue.claim("w2", ["rank"], 30)
    assert (task.id, task.attempts) == (task_id, 2)
    row = _row(pg, task_id)
    assert row["locked_by"] == "w2"
    assert row["last_error"] == "lease expired (worker w1, attempt 1/3)"
    assert taskqueue.complete(task_id, "w1", {"late": True}) is False
    assert taskqueue.complete(task_id, "w2", {"ok": True}) is True


def test_reclaimed_task_without_attempts_left_is_marked_dead(pg):
    task_id = taskqueue.enqueue("rank", {}, max_attempts=1)
    other = taskqueue.enqueue("rank", {})
    assert taskqueue.claim("w1", ["rank"], 30).id == task_id
    _expire_lease(pg, task_id)
    # w2 doesn't get the exhausted task; it is killed and w2 gets the next one
    assert taskqueue.claim("w2", ["rank"], 30).id == other
    row = _row(pg, task_id)
    assert (row["status"], row["attempts"]) == ("dead", 1)
    assert "lease expired (worker w1, attempt 1/1)" in row["last_error"]
    assert row["finished_at"] is not None
    assert taskqueue.complete(task_id, "w1", {}) is False


# ---- fail(): backoff, then dead ------------------------------------------------------

def test_fail_requeues_with_backoff_until_max_attempts_then_dead(pg):
    task_id = taskqueue.enqueue("boom", {}, max_attempts=2)
    taskqueue.claim("w1", ["boom"], 30)
    assert taskqueue.fail(task_id, "w1", "first failure") is True
    row = _row(pg, task_id)
    assert (row["status"], row["attempts"], row["locked_by"]) == ("queued", 1, None)
    assert row["last_error"] == "first failure" and row["finished_at"] is None
    delay = (row["run_after"] - row["db_now"]).total_seconds()
    base = taskqueue.BACKOFF_BASE_S
    assert base / 2 - 1 <= delay <= base, delay            # future, jittered
    assert taskqueue.claim("w1", ["boom"], 30) is None      # backing off

    _make_due(pg, task_id)
    assert taskqueue.claim("w1", ["boom"], 30).attempts == 2
    assert taskqueue.fail(task_id, "w1", "second failure") is True
    row = _row(pg, task_id)
    assert (row["status"], row["attempts"]) == ("dead", 2)
    assert row["last_error"] == "second failure" and row["finished_at"] is not None
    _make_due(pg, task_id)
    assert taskqueue.claim("w1", ["boom"], 30) is None      # dead stays dead


def test_long_errors_are_truncated(pg):
    task_id = taskqueue.enqueue("boom", {}, max_attempts=1)
    taskqueue.claim("w1", ["boom"], 30)
    taskqueue.fail(task_id, "w1", "x" * 20000)
    err = _row(pg, task_id)["last_error"]
    assert len(err) <= taskqueue.core.MAX_ERROR_CHARS
    assert err.endswith("[truncated]")


def test_backoff_is_capped(pg, monkeypatch):
    monkeypatch.setattr(taskqueue.core, "BACKOFF_CAP_S", 7.0)
    task_id = taskqueue.enqueue("boom", {}, max_attempts=10)
    with pg.begin() as conn:                       # as if 6 attempts had failed
        conn.execute(text("UPDATE tasks SET attempts = 6 WHERE id = :i"),
                     {"i": task_id})
    taskqueue.claim("w1", ["boom"], 30)
    assert taskqueue.fail(task_id, "w1", "again")
    row = _row(pg, task_id)
    assert (row["run_after"] - row["db_now"]).total_seconds() <= 7.0
