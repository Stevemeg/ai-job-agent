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
        # exactly one event_type constraint, still under the canonical name
        # (plus Phase 5's source check)
        checks = conn.execute(text(
            "SELECT conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'events' AND c.contype = 'c' "
            "AND t.relnamespace = current_schema()::regnamespace "
            "ORDER BY conname")).scalars().all()
    assert types == ["applied", "approved", "declined", "drafted"]   # old row kept
    assert checks == [db.EVENT_TYPE_CHECK_NAME, db.EVENT_SOURCE_CHECK_NAME]


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
# Full definitions (incl. the partial-index WHERE) for the v2 tables, which
# must match exactly between schema.sql and the ORM.
_TASK_INDEXDEFS_SQL = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = current_schema()
  AND tablename IN ('tasks', 'job_scores', 'applications', 'application_artifacts',
                    'approvals', 'application_actions', 'agent_memories')
ORDER BY indexname
"""
# ...and their constraints (PK, FK with its ON DELETE, CHECKs).
_V2_CONSTRAINTS_SQL = """
SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid)
FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
WHERE t.relnamespace = current_schema()::regnamespace
  AND (t.relname IN ('tasks', 'job_scores', 'applications', 'application_artifacts',
                     'approvals', 'application_actions', 'agent_memories')
       OR c.conname = 'events_source_check')
ORDER BY 1, 2
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
        task_idx_from_sql = conn.execute(text(_TASK_INDEXDEFS_SQL)).all()
        v2_cons_from_sql = conn.execute(text(_V2_CONSTRAINTS_SQL)).all()

    # schema.sql-built databases must also survive migrations untouched
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        assert conn.execute(text(_COLUMNS_SQL)).all() == from_sql

    with pg.begin() as conn:
        conn.execute(text(
            "DROP TABLE agent_memories, application_actions, approvals, "
            "application_artifacts, applications, job_scores, events, canonical_jobs, "
            "users, tasks"))
    monkeypatch.setattr(db, "_initialized", False)
    db.init_db()
    with pg.connect() as conn:
        from_orm = conn.execute(text(_COLUMNS_SQL)).all()
        idx_from_orm = conn.execute(text(_INDEXES_SQL)).scalars().all()
        task_idx_from_orm = conn.execute(text(_TASK_INDEXDEFS_SQL)).all()
        v2_cons_from_orm = conn.execute(text(_V2_CONSTRAINTS_SQL)).all()

    assert _normalized(from_orm) == _normalized(from_sql)
    # ORM-only extras would be fine, but every schema.sql index must exist
    # on ORM-built databases too (constraint-backing indexes share names).
    assert set(idx_from_sql) - set(idx_from_orm) <= {
        "idx_canonical_jobs_company_title", "idx_events_user_id",
        "idx_events_job_id", "idx_events_event_type"}
    assert task_idx_from_orm == task_idx_from_sql
    assert db.TASK_CONCURRENCY_INDEX in dict(task_idx_from_sql)
    assert db.JOB_SCORES_LOOKUP_INDEX in dict(task_idx_from_sql)
    assert db.MEMORY_UNIQUE_INDEX in dict(task_idx_from_sql)
    assert v2_cons_from_orm == v2_cons_from_sql
    assert ("job_scores", "job_scores_pkey",
            "PRIMARY KEY (job_id, profile_hash, cache_key)") in v2_cons_from_sql
    names = {(t, n) for t, n, _ in v2_cons_from_sql}
    assert {("applications", "applications_user_id_job_id_key"),
            ("applications", "applications_status_check"),
            ("applications", "applications_thread_id_key"),
            ("applications", "applications_job_id_fkey"),
            ("application_artifacts", "application_artifacts_application_id_kind_key"),
            ("application_artifacts", "application_artifacts_kind_check"),
            ("application_artifacts", "application_artifacts_application_id_fkey"),
            ("approvals", "approvals_application_id_application_version_key"),
            ("approvals", "approvals_decision_check"),
            ("approvals", "approvals_source_check"),
            ("approvals", "approvals_expire_source_check"),
            ("application_actions", "application_actions_application_id_action_key"),
            ("application_actions", "application_actions_action_check"),
            ("application_actions", "application_actions_status_check"),
            ("events", "events_source_check"),
            ("agent_memories", "agent_memories_kind_check"),
            ("agent_memories", "agent_memories_provenance_check"),
            ("agent_memories", "agent_memories_user_id_fkey"),
            } <= names


# The tasks table exactly as Phase 1 shipped it (before the queue columns).
PHASE1_TASKS = """
CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'dead')),
    idempotency_key TEXT UNIQUE,
    attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 3,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ, locked_by TEXT, last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX idx_tasks_status_run_after ON tasks (status, run_after);
INSERT INTO tasks (kind) VALUES ('rank'), ('rank');
"""


def test_phase1_tasks_table_gains_queue_columns_and_index(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(OLD_SCHEMA + PHASE1_TASKS)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        cols = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'tasks'"
        )).scalars())
        indexdef = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = :n"), {"n": db.TASK_CONCURRENCY_INDEX}).scalar()
        rows = conn.execute(text(
            "SELECT kind, concurrency_key, result FROM tasks ORDER BY id")).all()
    assert {"concurrency_key", "result", "started_at", "finished_at"} <= cols
    assert indexdef and "UNIQUE" in indexdef and "WHERE" in indexdef
    assert rows == [("rank", None, None), ("rank", None, None)]   # kept


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


# A database exactly as Phase 2 left it: today's schema.sql minus job_scores.
PHASE2_SCHEMA = SCHEMA_SQL.read_text(encoding="utf-8").split(
    "-- v2 incremental ranking (Phase 3)")[0]


def test_phase2_database_gains_job_scores_idempotently(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(PHASE2_SCHEMA)
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title, "
            "content_hash) VALUES ('acme', 'AI Engineer', 'ai engineer', 'h')"))
        assert conn.execute(text(
            "SELECT to_regclass('job_scores') IS NULL")).scalar()
    _run_init_twice(monkeypatch)

    def score(passed=True, profile="p1", **cols):
        values = {"match_score": 71.5, "score_breakdown": '{"semantic": 0.5}',
                  "explanation": '{"strong_matches": []}', **cols}
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO job_scores (job_id, profile_hash, cache_key, "
                "content_hash, passed, match_score, score_breakdown, explanation) "
                "VALUES (1, :p, '1', 'c', :passed, :match_score, "
                "CAST(:score_breakdown AS jsonb), CAST(:explanation AS jsonb))"),
                {"p": profile, "passed": passed, **values})

    score()
    score(profile="p2", passed=False, match_score=None, score_breakdown=None,
          explanation=None)                                 # gate failure row
    with pytest.raises(IntegrityError):
        score()                                             # primary key
    with pytest.raises(IntegrityError):                     # passed needs a score
        score(profile="p3", match_score=None)
    with pg.connect() as conn:
        indexdef = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = :n"), {"n": db.JOB_SCORES_LOOKUP_INDEX}).scalar()
        assert conn.execute(text("SELECT count(*) FROM job_scores")).scalar() == 2
    assert indexdef and "(profile_hash, cache_key)" in indexdef

    with pg.begin() as conn:                                # FK cascades
        conn.execute(text("DELETE FROM canonical_jobs"))
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM job_scores")).scalar() == 0


# job_scores exactly as Phase 3 first shipped it: keyed on the bare
# FORMULA_VERSION.
PHASE3_JOB_SCORES = """
CREATE TABLE job_scores (
    job_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    profile_hash TEXT NOT NULL, formula_version TEXT NOT NULL,
    content_hash TEXT NOT NULL, passed BOOLEAN NOT NULL, match_score NUMERIC,
    score_breakdown JSONB, explanation JSONB,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, profile_hash, formula_version),
    CONSTRAINT job_scores_passed_has_score_check CHECK (
        NOT passed OR (match_score IS NOT NULL AND score_breakdown IS NOT NULL
                       AND explanation IS NOT NULL)));
CREATE INDEX idx_job_scores_profile_formula
    ON job_scores (profile_hash, formula_version);
INSERT INTO canonical_jobs (company, title, normalized_title, content_hash)
    VALUES ('acme', 'AI Engineer', 'ai engineer', 'h');
INSERT INTO job_scores (job_id, profile_hash, formula_version, content_hash,
                        passed) VALUES (1, 'p1', '1', 'c', false);
"""


def test_phase3_job_scores_is_rekeyed_on_cache_key(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(PHASE2_SCHEMA + PHASE3_JOB_SCORES)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        cols = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'job_scores'"
        )).scalars())
        pkey = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'job_scores_pkey' "
            "AND connamespace = current_schema()::regnamespace")).scalar()
        indexdef = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = :n"), {"n": db.JOB_SCORES_LOOKUP_INDEX}).scalar()
        rows = conn.execute(text(
            "SELECT job_id, profile_hash, cache_key FROM job_scores")).all()
    assert "cache_key" in cols and "formula_version" not in cols
    assert pkey == "PRIMARY KEY (job_id, profile_hash, cache_key)"
    assert "(profile_hash, cache_key)" in indexdef
    assert rows == [(1, "p1", "1")]          # kept; it just never matches again


# A database exactly as Phase 3 left it: today's schema.sql minus the agent
# tables.
PHASE3_SCHEMA = SCHEMA_SQL.read_text(encoding="utf-8").split(
    "-- v2 agent (Phase 4")[0]


def test_phase3_database_gains_the_agent_tables_idempotently(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(PHASE3_SCHEMA)
        conn.execute(text("INSERT INTO users (email) VALUES ('u@x')"))
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title, "
            "content_hash) VALUES ('acme', 'AI Engineer', 'ai engineer', 'h'), "
            "('acme', 'ML Engineer', 'ml engineer', 'h2')"))
        assert conn.execute(text(
            "SELECT to_regclass('applications') IS NULL")).scalar()
    _run_init_twice(monkeypatch)

    def application(status="queued", thread="application:1", job=1):
        with pg.begin() as conn:
            return conn.execute(text(
                "INSERT INTO applications (user_id, job_id, profile_hash, cache_key, "
                "match_score, status, thread_id) VALUES (1, :j, 'p', 'k', 88.25, :s, :t) "
                "RETURNING id"), {"s": status, "t": thread, "j": job}).scalar()

    def artifact(kind, app_id=1):
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO application_artifacts (application_id, kind, path, "
                "sha256, report) VALUES (:a, :k, '/x', 'abc', '{\"ok\": true}')"),
                {"a": app_id, "k": kind})

    app_id = application()
    with pytest.raises(IntegrityError):
        application(thread="application:2")              # UNIQUE (user_id, job_id)
    with pytest.raises(IntegrityError) as exc:
        application(status="sent", thread="application:3", job=2)
    assert "applications_status_check" in str(exc.value)  # status CHECK
    with pytest.raises(IntegrityError):
        application(thread="application:1", job=2)        # UNIQUE thread_id
    assert application(status="awaiting_approval", thread="application:4", job=2)
    for kind in db.ARTIFACT_KINDS:
        artifact(kind, app_id)
    with pytest.raises(IntegrityError):
        artifact("resume_docx", app_id)                   # UNIQUE (application_id, kind)
    with pytest.raises(IntegrityError):
        artifact("email_draft", app_id)                   # kind CHECK
    with pg.begin() as conn:                              # FK cascades
        conn.execute(text("DELETE FROM canonical_jobs"))
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM applications")).scalar() == 0
        assert conn.execute(text(
            "SELECT count(*) FROM application_artifacts")).scalar() == 0


# The agent tables exactly as Phase 4 shipped them (before Phase 5).
PHASE4_AGENT_TABLES = """
CREATE TABLE IF NOT EXISTS applications (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    profile_hash    TEXT NOT NULL,
    cache_key       TEXT NOT NULL,
    match_score     NUMERIC(5,2),
    status          TEXT NOT NULL DEFAULT 'queued' CONSTRAINT applications_status_check
                        CHECK (status IN ('queued', 'drafting', 'drafted',
                                          'needs_review', 'failed')),
    thread_id       TEXT NOT NULL UNIQUE,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT applications_user_id_job_id_key UNIQUE (user_id, job_id)
);
CREATE TABLE IF NOT EXISTS application_artifacts (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CONSTRAINT application_artifacts_kind_check
                        CHECK (kind IN ('resume_docx', 'cover_letter_docx',
                                        'cover_letter_txt', 'jd_requirements')),
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    report          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT application_artifacts_application_id_kind_key UNIQUE (application_id, kind)
);
"""
PHASE4_SCHEMA = PHASE3_SCHEMA + PHASE4_AGENT_TABLES


def _seed_phase4(pg):
    with pg.begin() as conn:
        conn.exec_driver_sql(PHASE4_SCHEMA)
        # events.source is Phase 5 (PHASE3_SCHEMA is today's file's prefix).
        conn.execute(text("ALTER TABLE events DROP COLUMN source"))
        conn.execute(text("INSERT INTO users (email) VALUES ('u@x')"))
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title, "
            "content_hash, location, apply_link) VALUES "
            "('acme', 'AI Engineer', 'ai engineer', 'h', 'Pune', 'https://x/1'), "
            "('acme', 'ML Engineer', 'ml engineer', 'h2', 'Pune', 'https://x/2'), "
            "('acme', 'CV Engineer', 'cv engineer', 'h3', 'Pune', 'https://x/3')"))
        conn.execute(text(
            "INSERT INTO applications (user_id, job_id, profile_hash, cache_key, "
            "match_score, status, thread_id) VALUES "
            "(1, 1, 'p', 'k', 88.25, 'drafted', 'application:1'), "
            "(1, 2, 'p', 'k', 70, 'needs_review', 'application:2'), "
            "(1, 3, 'p', 'k', 60, 'drafting', 'application:3')"))


def test_phase4_applications_gain_the_matched_posting(pg, monkeypatch):
    from backend.agent import store
    _seed_phase4(pg)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM applications WHERE posting IS NULL")).scalar() == 3
    # Reads fall back to the job's own location and link without a posting.
    app = store.get_application(1)
    assert (app["location"], app["apply_link"]) == ("Pune", "https://x/1")


def test_phase4_database_moves_to_the_approval_states_idempotently(pg, monkeypatch):
    _seed_phase4(pg)
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, status, needs_review, version FROM applications "
            "ORDER BY id")).all()
        expiries = conn.execute(text(
            "SELECT payload, idempotency_key, "
            "run_after > now() + interval '6 days' AS later FROM tasks "
            "WHERE kind = 'approval_expiry' ORDER BY id")).all()
        tables = conn.execute(text(
            "SELECT to_regclass('approvals') IS NOT NULL, "
            "to_regclass('application_actions') IS NOT NULL")).one()
        source_col = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
            "current_schema() AND table_name = 'events' AND column_name = 'source'"
        )).scalar()
    # drafted -> awaiting, not flagged; needs_review -> awaiting, flagged;
    # still drafting -> untouched (version 0: nothing drafted yet).
    assert [tuple(r) for r in rows] == [(1, "awaiting_approval", False, 1),
                                        (2, "awaiting_approval", True, 1),
                                        (3, "drafting", False, 0)]
    # Each migrated row got its approval expiry -- once, despite two runs.
    assert [(e.payload, e.idempotency_key, e.later) for e in expiries] == [
        ({"application_id": 1, "version": 1}, "expiry:1:1", True),
        ({"application_id": 2, "version": 1}, "expiry:2:1", True)]
    assert tables == (True, True) and source_col == 1

    def status(value, app_id=3):
        with pg.begin() as conn:
            conn.execute(text("UPDATE applications SET status = :s WHERE id = :i"),
                         {"s": value, "i": app_id})
    for legacy in ("drafted", "needs_review"):
        with pytest.raises(IntegrityError):
            status(legacy)
    for current in db.APPLICATION_STATUSES:
        status(current)
    with pg.begin() as conn:
        conn.execute(text(
            "INSERT INTO application_artifacts (application_id, kind, path, sha256) "
            "VALUES (1, 'packet', '/p.json', 'abc')"))
    with pytest.raises(IntegrityError):
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO events (user_id, job_id, event_type, source) "
                "VALUES (1, 1, 'declined', 'someone')"))


def test_approvals_and_actions_constraints(pg):
    db.init_db()
    with pg.begin() as conn:
        conn.execute(text("INSERT INTO users (email) VALUES ('u@x')"))
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title) "
            "VALUES ('acme', 'AI Engineer', 'ai engineer')"))
        conn.execute(text(
            "INSERT INTO applications (user_id, job_id, profile_hash, cache_key, "
            "status, thread_id, version) VALUES (1, 1, 'p', 'k', "
            "'awaiting_approval', 'application:1', 1)"))

    def approval(decision, source, version, ok=True):
        stmt = text("INSERT INTO approvals (application_id, decision, source, "
                    "application_version) VALUES (1, :d, :s, :v)")
        if ok:
            with pg.begin() as conn:
                conn.execute(stmt, {"d": decision, "s": source, "v": version})
            return
        with pytest.raises(IntegrityError):
            with pg.begin() as conn:
                conn.execute(stmt, {"d": decision, "s": source, "v": version})

    approval("edit", "user", 1)
    approval("approve", "user", 1, ok=False)       # one decision per version
    approval("approve", "user", 2)
    approval("expire", "user", 3, ok=False)        # an expiry is never a person's
    approval("decline", "expiry", 3, ok=False)     # ...and a person never expires
    approval("send", "user", 3, ok=False)          # decision CHECK
    approval("expire", "expiry", 3)

    def action(kind, status="pending", ok=True):
        stmt = text("INSERT INTO application_actions (application_id, action, status) "
                    "VALUES (1, :a, :s)")
        if ok:
            with pg.begin() as conn:
                conn.execute(stmt, {"a": kind, "s": status})
            return
        with pytest.raises(IntegrityError):
            with pg.begin() as conn:
                conn.execute(stmt, {"a": kind, "s": status})

    action("packet", "done")
    action("packet", ok=False)                     # one row per action
    action("gmail_send", ok=False)                 # there is no send action
    action("gmail_draft", "sent", ok=False)        # status CHECK
    action("gmail_draft")
    with pg.begin() as conn:                       # FK cascades
        conn.execute(text("DELETE FROM applications"))
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM approvals")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM application_actions")).scalar() == 0


# A database exactly as Phase 5 left it: today's schema.sql without the agent
# memory table and without applications.memory_effects.
PHASE5_SCHEMA = SCHEMA_SQL.read_text(encoding="utf-8").split(
    "-- v2 agent memory (Phase 6")[0]


def test_phase5_database_gains_agent_memories_idempotently(pg, monkeypatch):
    with pg.begin() as conn:
        conn.exec_driver_sql(PHASE5_SCHEMA)
        conn.execute(text("ALTER TABLE applications DROP COLUMN memory_effects, "
                          "DROP COLUMN follow_up_due"))
        conn.execute(text(
            "ALTER TABLE application_actions DROP CONSTRAINT application_actions_action_check, "
            "ADD CONSTRAINT application_actions_action_check "
            "CHECK (action IN ('packet', 'gmail_draft'))"))
        conn.execute(text("INSERT INTO users (email) VALUES ('u@x'), ('v@x')"))
        conn.execute(text(
            "INSERT INTO canonical_jobs (company, title, normalized_title) "
            "VALUES ('acme', 'AI Engineer', 'ai engineer')"))
        conn.execute(text(
            "INSERT INTO applications (user_id, job_id, profile_hash, cache_key, "
            "status, thread_id, version) VALUES (1, 1, 'p', 'k', 'awaiting_approval', "
            "'application:1', 1)"))
        assert conn.execute(text("SELECT to_regclass('agent_memories') IS NULL")).scalar()
    _run_init_twice(monkeypatch)
    with pg.connect() as conn:
        assert conn.execute(text(
            "SELECT memory_effects FROM applications WHERE id = 1")).scalar() is None
        assert conn.execute(text(
            "SELECT follow_up_due FROM applications WHERE id = 1")).scalar() is False
    for action in db.ACTION_KINDS:                         # incl. gmail_follow_up
        with pg.begin() as conn:
            conn.execute(text("INSERT INTO application_actions (application_id, action) "
                              "VALUES (1, :a)"), {"a": action})
    with pytest.raises(IntegrityError):
        with pg.begin() as conn:
            conn.execute(text("INSERT INTO application_actions (application_id, action) "
                              "VALUES (1, 'gmail_send')"))

    def memory(kind="company_avoid", value="Acme", user=1, provenance="stated"):
        with pg.begin() as conn:
            return conn.execute(text(
                "INSERT INTO agent_memories (user_id, kind, value, provenance) "
                "VALUES (:u, :k, :v, :p) RETURNING id"),
                {"u": user, "k": kind, "v": value, "p": provenance}).scalar()

    memory()
    with pytest.raises(IntegrityError) as exc:
        memory(value="ACME")                               # case-insensitive key
    assert db.MEMORY_UNIQUE_INDEX in str(exc.value)
    assert memory(kind="company_prefer", value="acme")     # another kind: fine
    assert memory(user=2)                                  # another user: fine
    for kind in db.MEMORY_KINDS:
        memory(kind=kind, value=f"v-{kind}")
    with pytest.raises(IntegrityError):
        memory(kind="salary_floor", value="x")
    with pytest.raises(IntegrityError):
        memory(value="other", provenance="guessed")
    with pg.connect() as conn:
        row = conn.execute(text(
            "SELECT evidence, weight, active FROM agent_memories WHERE id = 1")).one()
    assert (row.evidence, float(row.weight), row.active) == ({}, 1.0, True)
    with pg.begin() as conn:                               # FK cascades
        conn.execute(text("DELETE FROM users WHERE id = 2"))
    with pg.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM agent_memories WHERE user_id = 2")).scalar() == 0
