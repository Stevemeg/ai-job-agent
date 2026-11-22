"""Migrations must upgrade a database created by an OLDER schema.sql in
place, idempotently, without losing rows. Each test runs in a throwaway
schema (see the ``pg`` fixture in conftest.py)."""
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402
from sqlalchemy.exc import IntegrityError         # noqa: E402

from backend.database import db                   # noqa: E402

pytestmark = pytest.mark.db

# The v1 DDL as first shipped: inline (auto-named) CHECK with the original
# eight event types, no events.note, none of the v2 canonical_jobs columns.
OLD_SCHEMA = """
CREATE TABLE users (
    id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE canonical_jobs (
    id SERIAL PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
    normalized_title TEXT NOT NULL, location TEXT, tags TEXT[],
    experience TEXT, seniority TEXT, clean_description TEXT,
    apply_link TEXT, source TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, normalized_title));
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'impression','click','save','dismiss',
        'applied','interview','rejected','offer')),
    match_score NUMERIC(5,2), score_breakdown JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO users (email) VALUES ('old@example.com');
INSERT INTO canonical_jobs (company, title, normalized_title)
    VALUES ('acme', 'AI Engineer', 'ai engineer');
INSERT INTO events (user_id, job_id, event_type) VALUES (1, 1, 'applied');
"""


def _insert_event(engine, event_type):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO events (user_id, job_id, event_type) "
            "VALUES (1, 1, :t)"), {"t": event_type})


def _run_init_twice(monkeypatch):
    for _ in range(2):
        monkeypatch.setattr(db, "_initialized", False)
        db.init_db()


def test_old_constraint_upgraded_idempotently(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(OLD_SCHEMA)

    # Before migrating, the old constraint really does reject agent types.
    with pytest.raises(IntegrityError):
        _insert_event(pg, "approved")

    _run_init_twice(monkeypatch)

    for agent_type in db.AGENT_EVENT_TYPES:
        _insert_event(pg, agent_type)
    with pytest.raises(IntegrityError):
        _insert_event(pg, "ghosted")

    with pg.connect() as conn:
        types = [r[0] for r in conn.execute(
            text("SELECT event_type FROM events ORDER BY id"))]
        # exactly one constraint, still under the canonical name
        checks = conn.execute(text(
            "SELECT conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'events' AND c.contype = 'c' "
            "AND t.relnamespace = current_schema()::regnamespace")).scalars().all()
    assert types == ["applied", "approved", "declined", "drafted"]   # old row kept
    assert checks == [db.EVENT_TYPE_CHECK_NAME]


def test_old_database_gains_v2_columns_and_tasks_table(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(OLD_SCHEMA)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        note_col = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'events' AND column_name = 'note'")).scalar()
        tasks_exists = conn.execute(text(
            "SELECT to_regclass('tasks') IS NOT NULL")).scalar()
        row = conn.execute(text(
            "SELECT postings, content_hash, content_changed_at, first_seen_at "
            "FROM canonical_jobs WHERE id = 1")).one()
    assert note_col == 1
    assert tasks_exists
    assert row.postings == []                 # backfilled by the column default
    assert row.content_hash is None and row.content_changed_at is None
    assert row.first_seen_at is not None      # pre-existing data untouched


SCHEMA_SQL = Path(__file__).resolve().parents[1] / "backend" / "database" / "schema.sql"

_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = current_schema()
ORDER BY table_name, column_name
"""
_INDEXES_SQL = """
SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()
ORDER BY indexname
"""

# Known v1 drift that pre-dates v2 and is harmless in Postgres: the v1 models
# use String (VARCHAR without length == TEXT) where schema.sql says TEXT, and
# leave their server-defaulted timestamps nullable where schema.sql says NOT
# NULL. Tolerated here so the test guards everything v2 adds; anything else
# (a missing column, a v2 type or nullability mismatch) still fails.
_V1_DEFAULTED_TIMESTAMPS = {("users", "created_at"), ("events", "created_at"),
                            ("canonical_jobs", "first_seen_at"),
                            ("canonical_jobs", "last_seen_at")}


def _normalized(rows):
    out = []
    for table, column, dtype, nullable in rows:
        if dtype == "character varying":
            dtype = "text"
        if (table, column) in _V1_DEFAULTED_TIMESTAMPS:
            nullable = "v1-drift"
        out.append((table, column, dtype, nullable))
    return out


def test_schema_sql_and_orm_models_stay_in_sync(pg, monkeypatch):
    """db.py says its models 'mirror schema.sql exactly'; this enforces it.
    A fresh Docker volume is built from schema.sql, everything else from
    create_all() -- the two must produce the same tables."""
    with pg.begin() as conn:
        conn.exec_driver_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    with pg.connect() as conn:
        from_sql = conn.execute(text(_COLUMNS_SQL)).all()
        idx_from_sql = conn.execute(text(_INDEXES_SQL)).scalars().all()

    # schema.sql-built databases must also survive migrations untouched
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        assert conn.execute(text(_COLUMNS_SQL)).all() == from_sql

    with pg.begin() as conn:
        conn.execute(text("DROP TABLE events, canonical_jobs, users, tasks"))
    monkeypatch.setattr(db, "_initialized", False)
    db.init_db()
    with pg.connect() as conn:
        from_orm = conn.execute(text(_COLUMNS_SQL)).all()
        idx_from_orm = conn.execute(text(_INDEXES_SQL)).scalars().all()

    assert _normalized(from_orm) == _normalized(from_sql)
    # ORM-only extras would be fine, but every schema.sql index must exist
    # on ORM-built databases too (constraint-backing indexes share names).
    assert set(idx_from_sql) - set(idx_from_orm) <= {
        "idx_canonical_jobs_company_title", "idx_events_user_id",
        "idx_events_job_id", "idx_events_event_type"}


def _event_check_oid(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT c.oid FROM pg_constraint c "
            "WHERE c.conrelid = 'events'::regclass AND c.conname = :n"),
            {"n": db.EVENT_TYPE_CHECK_NAME}).scalar()


def _migrate(monkeypatch):
    monkeypatch.setattr(db, "_initialized", False)
    db.init_db()


@pytest.mark.parametrize("built_from", ["orm", "schema_sql", "old_schema"])
def test_rerunning_migrations_never_rewrites_the_events_check(
        pg, monkeypatch, built_from):
    """Only a stale constraint is dropped and re-added; once current, every
    later run leaves the same pg_constraint row (same oid) in place."""
    if built_from == "schema_sql":
        with pg.begin() as conn:
            conn.exec_driver_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    elif built_from == "old_schema":
        with pg.begin() as conn:
            conn.exec_driver_sql(OLD_SCHEMA)
    before = _event_check_oid(pg) if built_from != "orm" else None

    _migrate(monkeypatch)
    first = _event_check_oid(pg)
    if built_from == "old_schema":
        assert first != before                     # stale -> rewritten once
    elif built_from == "schema_sql":
        assert first == before                     # already current

    _migrate(monkeypatch)
    db._run_migrations()                           # the bare entry point too
    assert _event_check_oid(pg) == first


def test_migrations_take_the_advisory_lock(pg, monkeypatch):
    """A second migrator blocks on pg_advisory_xact_lock while the first
    holds it (no interleaved DDL), then completes once it is released."""
    import threading
    db.init_db()
    holder = pg.connect()
    trans = holder.begin()
    holder.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": db._MIGRATION_LOCK_KEY})
    done = threading.Event()
    errors = []

    def migrate():
        try:
            db._run_migrations()
        except Exception as exc:                   # noqa: BLE001
            errors.append(exc)
        done.set()

    t = threading.Thread(target=migrate)
    t.start()
    try:
        assert not done.wait(1.0)                  # blocked behind the lock
    finally:
        trans.rollback()                           # releases the xact lock
        holder.close()
    t.join(10)
    assert done.is_set() and not errors


def test_fresh_database_init_is_idempotent(pg, monkeypatch):
    _run_init_twice(monkeypatch)
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO users (email) VALUES ('a@b.c')"))
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title) "
            "VALUES ('acme', 'X', 'x')"))
    for agent_type in db.AGENT_EVENT_TYPES:
        _insert_event(pg, agent_type)
    with pytest.raises(IntegrityError):
        _insert_event(pg, "ghosted")
