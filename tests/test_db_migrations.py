"""Migrations must upgrade a database created by an OLDER schema.sql in
place, idempotently, without losing rows. Each test runs in a throwaway
schema (see the ``pg`` fixture in conftest.py)."""
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


def test_old_events_gain_note_column(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(OLD_SCHEMA)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        note_col = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'events' AND column_name = 'note'")).scalar()
    assert note_col == 1


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
