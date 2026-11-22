"""tasks table: schema-level guarantees the queue and worker rely on."""
import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402
from sqlalchemy.exc import IntegrityError         # noqa: E402

from backend.database import db                   # noqa: E402

pytestmark = pytest.mark.db


def _enqueue(engine, kind="rank", key=None, status=None, ckey=None):
    cols, vals = ["kind", "idempotency_key"], [":kind", ":key"]
    if status is not None:
        cols.append("status")
        vals.append(":status")
    if ckey is not None:
        cols.append("concurrency_key")
        vals.append(":ckey")
    with engine.begin() as conn:
        return conn.execute(text(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join(vals)}) "
            "RETURNING id"), {"kind": kind, "key": key, "status": status,
                              "ckey": ckey}).scalar()


def test_idempotency_key_uniqueness_enforced(pg):
    db.init_db()
    _enqueue(pg, key="rank:2026-11-22")
    with pytest.raises(IntegrityError):
        _enqueue(pg, key="rank:2026-11-22")
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 1


def test_null_idempotency_keys_never_collide(pg):
    db.init_db()
    _enqueue(pg, key=None)
    _enqueue(pg, key=None)
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 2


def test_defaults_and_status_check(pg):
    db.init_db()
    task_id = _enqueue(pg)
    with pg.connect() as conn:
        row = conn.execute(text("SELECT * FROM tasks WHERE id = :i"),
                           {"i": task_id}).mappings().one()
    assert row["status"] == "queued"
    assert row["payload"] == {}
    assert (row["attempts"], row["max_attempts"]) == (0, 3)
    assert row["run_after"] is not None and row["updated_at"] is not None
    assert row["lease_expires_at"] is None and row["locked_by"] is None

    for status in db.TASK_STATUSES:
        _enqueue(pg, status=status)
    with pytest.raises(IntegrityError):
        _enqueue(pg, status="paused")


def test_claim_index_exists(pg):
    db.init_db()
    with pg.connect() as conn:
        indexdef = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = 'idx_tasks_status_run_after'")).scalar()
    assert indexdef and "(status, run_after)" in indexdef


def test_concurrency_key_allows_one_active_task(pg):
    db.init_db()
    _enqueue(pg, ckey="rank:default")                       # queued
    with pytest.raises(IntegrityError):
        _enqueue(pg, ckey="rank:default")
    with pytest.raises(IntegrityError):
        _enqueue(pg, ckey="rank:default", status="running")
    # terminal tasks don't hold the key; other keys and NULL are independent
    _enqueue(pg, ckey="rank:default", status="succeeded")
    _enqueue(pg, ckey="rank:default", status="dead")
    _enqueue(pg, ckey="scan:default")
    _enqueue(pg)
    _enqueue(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET status = 'succeeded' "
                          "WHERE concurrency_key = 'rank:default' "
                          "AND status = 'queued'"))
    _enqueue(pg, ckey="rank:default")                       # free again
