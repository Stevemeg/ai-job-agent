"""
db.py — Postgres connection layer + ORM models for users, canonical_jobs,
events, tasks, job_scores, applications, application_artifacts, approvals,
application_actions and agent_memories.

Connection string is read from environment variables (see .env.example).
Never hardcode credentials here -- this file is committed to your repo.

Usage:
    from backend.db.db import get_session, User, CanonicalJob, Event

    with get_session() as session:
        user = session.query(User).filter_by(email="...").first()
"""
import os
import re
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, Boolean, String, Text,
    ForeignKey, TIMESTAMP, Numeric, ARRAY, CheckConstraint, UniqueConstraint,
    Index, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()


def _build_db_url():
    """
    Build the SQLAlchemy database URL.

    Priority:
    1. DATABASE_URL (Render / Neon / Production)
    2. Individual DB_* variables (Local Docker / Development)
    """

    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        # Render + Neon provide this
        return database_url

    user = os.environ.get("DB_USER", "ai_job_agent")
    password = os.environ.get("DB_PASSWORD", "dev_password_change_me")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "ai_job_agent")

    return (
        f"postgresql+psycopg2://"
        f"{user}:{password}@{host}:{port}/{name}"
    )


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_build_db_url(), pool_pre_ping=True)
    return _engine


@contextmanager
def get_session():
    """Context-managed session: commits on success, rolls back on error,
    always closes. Use as: `with get_session() as session: ...`"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# MODELS — mirror schema.sql exactly. If you change one, change the other.
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True)
    display_name = Column(String)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    events = relationship("Event", back_populates="user")


class CanonicalJob(Base):
    __tablename__ = "canonical_jobs"
    __table_args__ = (
        UniqueConstraint("company", "normalized_title", name="canonical_jobs_company_normalized_title_key"),
    )

    id = Column(Integer, primary_key=True)
    company = Column(String, nullable=False)
    title = Column(String, nullable=False)
    normalized_title = Column(String, nullable=False)
    location = Column(String)
    tags = Column(ARRAY(Text))
    experience = Column(String)
    seniority = Column(String)
    clean_description = Column(Text)
    apply_link = Column(String)
    source = Column(String)
    first_seen_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_seen_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # v2 identity + change detection (written by database/corpus.py):
    # every ATS posting behind this canonical job, as
    # [{source, posting_id, location, apply_link}], deduped on
    # (source, posting_id); UNIQUE(company, normalized_title) is unchanged.
    postings = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # sha256 over normalized title + description + location set. NULL marks a
    # stub row (created by impression/outcome logging, never collected), which
    # is why the corpus loader only reads rows where this is set.
    content_hash = Column(Text)
    content_changed_at = Column(TIMESTAMP(timezone=True))

    events = relationship("Event", back_populates="job")

    @staticmethod
    def normalize_title(title):
        return " ".join((title or "").lower().split())


# v1 event types: auto-logged telemetry plus what a person logs by hand.
CORE_EVENT_TYPES = (
    "impression", "click", "save", "dismiss",
    "applied", "interview", "rejected", "offer",
)
# v2 agent decisions (ADR-001). Written automatically when the approval gate
# resolves; never offered for manual entry (CLI, tracker form, or the
# POST /v1/tracker/events schema).
AGENT_EVENT_TYPES = ("approved", "declined", "drafted")

VALID_EVENT_TYPES = CORE_EVENT_TYPES + AGENT_EVENT_TYPES

EVENT_TYPE_CHECK_NAME = "events_event_type_check"

# Who caused an agent event (Phase 5). NULL for v1 / manually logged events.
EVENT_SOURCES = ("user", "expiry", "agent")
EVENT_SOURCE_CHECK_NAME = "events_source_check"


def event_type_check_sql() -> str:
    """The CHECK body for events.event_type -- single source for the ORM
    constraint and the migration that rewrites it on existing databases."""
    return "event_type IN (%s)" % ",".join(f"'{t}'" for t in VALID_EVENT_TYPES)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(event_type_check_sql(), name=EVENT_TYPE_CHECK_NAME),
        CheckConstraint("source IN (%s)" % ", ".join(f"'{s}'" for s in EVENT_SOURCES),
                        name=EVENT_SOURCE_CHECK_NAME),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(Integer, ForeignKey("canonical_jobs.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String, nullable=False)
    match_score = Column(Numeric(5, 2))
    score_breakdown = Column(JSONB)
    note = Column(Text)          # free-text note (tracker UI); nullable
    source = Column(Text)        # EVENT_SOURCES for agent events; NULL otherwise
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="events")
    job = relationship("CanonicalJob", back_populates="events")


TASK_STATUSES = ("queued", "running", "succeeded", "failed", "dead")
# A concurrency_key is held while its task is in one of these states; the
# partial unique index below allows at most one such task per key.
TASK_ACTIVE_STATUSES = ("queued", "running")
TASK_CONCURRENCY_INDEX = "uq_tasks_concurrency_key_active"


def _task_active_predicate() -> str:
    return "status IN (%s)" % ", ".join(f"'{s}'" for s in TASK_ACTIVE_STATUSES)


class Task(Base):
    """Durable work queue row (ADR-001 §2), written and claimed by
    backend/taskqueue (SELECT ... FOR UPDATE SKIP LOCKED).

    updated_at is refreshed by the ORM on ORM updates; the queue's raw-SQL
    writers (claim/heartbeat/complete/fail) set it explicitly."""
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN (%s)" % ",".join(f"'{s}'" for s in TASK_STATUSES),
            name="tasks_status_check"),
        Index("idx_tasks_status_run_after", "status", "run_after"),
        Index(TASK_CONCURRENCY_INDEX, "concurrency_key", unique=True,
              postgresql_where=text(_task_active_predicate())),
    )

    id = Column(BigInteger, primary_key=True)            # BIGSERIAL
    kind = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(Text, nullable=False, server_default=text("'queued'"))
    idempotency_key = Column(Text, unique=True)          # NULLs never collide
    # At most one queued/running task per key (e.g. "rank:default"); NULL
    # means unconstrained. Freed as soon as the task succeeds or dies.
    concurrency_key = Column(Text)
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False, server_default=text("3"))
    run_after = Column(TIMESTAMP(timezone=True), nullable=False,
                       server_default=func.now())
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    locked_by = Column(Text)
    last_error = Column(Text)
    result = Column(JSONB)                               # set on success
    started_at = Column(TIMESTAMP(timezone=True))        # latest claim
    finished_at = Column(TIMESTAMP(timezone=True))       # succeeded / dead
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


JOB_SCORES_LOOKUP_INDEX = "idx_job_scores_profile_formula"


class JobScore(Base):
    """One job's cached matcher output for one profile under one scoring
    regime (Phase 3 incremental ranking; written by services/scoring.py
    while the rank lock is held).

    A job needs (re)scoring iff there is no row for (job_id, profile_hash,
    cache_key) whose content_hash is the job's current one. cache_key is
    scoring.cache_key(), "<FORMULA_VERSION>:<fingerprint[:12]>": it moves
    whenever the weights, gates, embedding model or matcher code do. Gate
    failures are rows too (passed = false, no score), so they are not
    re-evaluated either. content_hash is scoring.score_input_hash(): it
    covers every job field the matcher reads, including the tags and
    seniority that canonical_jobs.content_hash leaves out."""
    __tablename__ = "job_scores"
    __table_args__ = (
        # A passing row always carries its score (assembly relies on it).
        CheckConstraint(
            "NOT passed OR (match_score IS NOT NULL AND score_breakdown IS "
            "NOT NULL AND explanation IS NOT NULL)",
            name="job_scores_passed_has_score_check"),
        # Every rank execution reads all rows of one (profile, formula).
        Index(JOB_SCORES_LOOKUP_INDEX, "profile_hash", "cache_key"),
    )

    job_id = Column(Integer, ForeignKey("canonical_jobs.id", ondelete="CASCADE"),
                    primary_key=True)
    profile_hash = Column(Text, primary_key=True)      # sha256, canonical JSON
    cache_key = Column(Text, primary_key=True)         # scoring.cache_key()
    content_hash = Column(Text, nullable=False)
    passed = Column(Boolean, nullable=False)           # the hard gates
    match_score = Column(Numeric)                      # NULL when gated out
    # none_as_null: a gated-out row's None is SQL NULL, not JSON null.
    score_breakdown = Column(JSONB(none_as_null=True))  # {skill_overlap, semantic, role, seniority}
    explanation = Column(JSONB(none_as_null=True))      # {strong_matches, likely_matches, missing_skills}
    scored_at = Column(TIMESTAMP(timezone=True), nullable=False,
                       server_default=func.now())


# v2 agent (backend/agent). An application is one job the agent triaged
# for one user, drafts documents for, and then waits on the user for:
#   queued -> drafting -> awaiting_approval -> approved -> ready_to_apply
#                      |   (needs_review:   |            -> email_drafted
#                      |    any validator   -> declined
#                      |    flag)           -> expired   (nobody decided in time)
#                      -> failed             (its drafting task died; error says why)
# An edit re-drafts in place: still awaiting_approval, at version + 1.
APPLICATION_STATUSES = ("queued", "drafting", "awaiting_approval", "approved",
                        "declined", "expired", "ready_to_apply", "email_drafted",
                        "failed")
# Drafting finished: a dead drafting task never overwrites these.
APPLICATION_DONE_STATUSES = ("awaiting_approval", "approved", "declined", "expired",
                             "ready_to_apply", "email_drafted")
# Phase 4's final statuses; _run_migrations() moves such rows to
# awaiting_approval (needs_review = whether they were 'needs_review').
LEGACY_APPLICATION_STATUSES = ("drafted", "needs_review")
ARTIFACT_KINDS = ("resume_docx", "cover_letter_docx", "cover_letter_txt",
                  "jd_requirements", "packet")
APPROVAL_DECISIONS = ("approve", "decline", "edit", "expire")
APPROVAL_SOURCES = ("user", "expiry")
ACTION_KINDS = ("packet", "gmail_draft")
ACTION_STATUSES = ("pending", "done", "failed")


def _in_list(column: str, values) -> str:
    return "%s IN (%s)" % (column, ", ".join(f"'{v}'" for v in values))


class Application(Base):
    """One agent-drafted application packet (backend/agent). Created by the
    run graph's dispatch in the same transaction as its "application" task;
    the application graph (LangGraph thread `thread_id`) moves it through
    APPLICATION_STATUSES. One per (user, job), ever: triage never offers a
    job that already has one."""
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="applications_user_id_job_id_key"),
        CheckConstraint(_in_list("status", APPLICATION_STATUSES),
                        name="applications_status_check"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(Integer, ForeignKey("canonical_jobs.id", ondelete="CASCADE"),
                    nullable=False)
    profile_hash = Column(Text, nullable=False)        # the profile it was triaged for
    cache_key = Column(Text, nullable=False)           # ...and the scores' cache key
    match_score = Column(Numeric(5, 2))                # score at triage
    status = Column(Text, nullable=False, server_default=text("'queued'"))
    thread_id = Column(Text, nullable=False, unique=True)   # "application:<id>"
    error = Column(Text)                               # why it failed
    # The posting triage matched on location ({source, posting_id, location,
    # apply_link}): the application uses its location and apply_link.
    # NULL: the job's own location matched (agent/locations.py).
    posting = Column(JSONB(none_as_null=True))
    # A validator flagged the current documents (shown in the review packet).
    needs_review = Column(Boolean, nullable=False, server_default=text("false"))
    # The documents' version: 0 until drafted, 1 for the first draft, +1 per
    # edit. Every decision names the version it was made on.
    version = Column(Integer, nullable=False, server_default=text("0"))
    # Phase 6: the agent memories that moved this job up the triage order,
    # {"boost", "memories": [...]}; NULL if none did (agent/memory.py).
    memory_effects = Column(JSONB(none_as_null=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


class ApplicationArtifact(Base):
    """A file the application graph rendered for an application, with its
    sha256 and the truthfulness validator's report. One row per (application,
    kind): a retried render overwrites it."""
    __tablename__ = "application_artifacts"
    __table_args__ = (
        UniqueConstraint("application_id", "kind",
                         name="application_artifacts_application_id_kind_key"),
        CheckConstraint(_in_list("kind", ARTIFACT_KINDS),
                        name="application_artifacts_kind_check"),
    )

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"),
                            nullable=False)
    kind = Column(Text, nullable=False)
    path = Column(Text, nullable=False)                # absolute, on the worker's disk
    sha256 = Column(Text, nullable=False)
    report = Column(JSONB(none_as_null=True))          # validator output, if any
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())


class Approval(Base):
    """One decision on one version of an application (Phase 5 approval
    gate): approve / decline / edit from the user (the decision API) or
    expire from the approval TTL. UNIQUE (application_id,
    application_version): a version is decided at most once."""
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("application_id", "application_version",
                         name="approvals_application_id_application_version_key"),
        CheckConstraint(_in_list("decision", APPROVAL_DECISIONS),
                        name="approvals_decision_check"),
        CheckConstraint(_in_list("source", APPROVAL_SOURCES),
                        name="approvals_source_check"),
        CheckConstraint("(decision = 'expire') = (source = 'expiry')",
                        name="approvals_expire_source_check"),
    )

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"),
                            nullable=False)
    decision = Column(Text, nullable=False)
    edits = Column(JSONB(none_as_null=True))           # decision 'edit': what changed
    reason = Column(Text)
    source = Column(Text, nullable=False)
    application_version = Column(Integer, nullable=False)
    decided_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())


class ApplicationAction(Base):
    """What executing an approved application did: the 'packet' and at
    most one 'gmail_draft' (never sent). The gmail_draft row is written
    'pending' with its deterministic Message-ID before Gmail is called, so
    a retry finds the draft instead of creating a second one."""
    __tablename__ = "application_actions"
    __table_args__ = (
        UniqueConstraint("application_id", "action",
                         name="application_actions_application_id_action_key"),
        CheckConstraint(_in_list("action", ACTION_KINDS),
                        name="application_actions_action_check"),
        CheckConstraint(_in_list("status", ACTION_STATUSES),
                        name="application_actions_status_check"),
    )

    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"),
                            nullable=False)
    action = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    external_id = Column(Text)                         # the Gmail draft id
    message_id = Column(Text)                          # RFC 5322 Message-ID
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


MEMORY_KINDS = ("company_avoid", "title_avoid", "company_prefer", "title_prefer",
                "location_prefer", "note")
MEMORY_PROVENANCES = ("stated", "learned")
MEMORY_UNIQUE_INDEX = "uq_agent_memories_user_kind_value"


class AgentMemory(Base):
    """A preference the agent was told (provenance 'stated', from the user)
    or learned from the user's decisions ('learned', with its evidence).
    Triage-only (backend/agent/memory.py): *_avoid excludes, *_prefer
    boosts the triage order; scores and job_scores are never touched.
    One per (user, kind, lower(value))."""
    __tablename__ = "agent_memories"
    __table_args__ = (
        CheckConstraint(_in_list("kind", MEMORY_KINDS), name="agent_memories_kind_check"),
        CheckConstraint(_in_list("provenance", MEMORY_PROVENANCES),
                        name="agent_memories_provenance_check"),
        Index(MEMORY_UNIQUE_INDEX, "user_id", "kind", text("lower(value)"), unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(Text, nullable=False)
    value = Column(Text, nullable=False)
    provenance = Column(Text, nullable=False)
    evidence = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    weight = Column(Numeric, nullable=False, server_default=text("1"))
    active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


_initialized = False

# pg_advisory_xact_lock key serializing schema setup across processes (API,
# UI and worker can all start at once against a fresh database).
# Transaction-scoped: released on commit/rollback. backend/agent/checkpoint.py
# takes the same key (session-level) around LangGraph's checkpointer setup().
_MIGRATION_LOCK_KEY = 0x6D696772   # b"migr"
MIGRATION_LOCK_KEY = _MIGRATION_LOCK_KEY


def init_db():
    """Creates all tables if they don't already exist and applies additive
    migrations. Idempotent AND memoized per process -- callers (tracker UI,
    API routes) may invoke this on every request without paying CREATE/ALTER
    round-trips each time (audit fix). create_all() and the migrations run
    in one transaction under the migration lock, so concurrent first
    starts can't race each other's CREATE TABLE."""
    global _initialized
    if _initialized:
        return
    with get_engine().begin() as conn:
        _take_migration_lock(conn)
        Base.metadata.create_all(conn)
        _run_migrations(conn)
    _initialized = True


def _take_migration_lock(conn):
    # Re-entrant within one session, so init_db -> _run_migrations is fine.
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                 {"k": _MIGRATION_LOCK_KEY})


_SQL_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def check_allows_exactly(constraint_def, column, values):
    """True if a pg_get_constraintdef() of an `column IN (...)` CHECK
    allows exactly `values`. Compares the value set, not the text:
    Postgres deparses IN (...) differently for a TEXT column (schema.sql)
    and a VARCHAR one (the ORM), so no single expected string fits both."""
    if not constraint_def or column not in constraint_def:
        return False
    found = [v.replace("''", "'") for v in _SQL_LITERAL.findall(constraint_def)]
    return sorted(found) == sorted(values)


def event_type_check_is_current(constraint_def):
    """True if a pg_get_constraintdef() of events_event_type_check allows
    exactly VALID_EVENT_TYPES."""
    return check_allows_exactly(constraint_def, "event_type", VALID_EVENT_TYPES)


def _check_def(conn, table, name):
    return conn.execute(text(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
        "AND c.conname = :name"), {"table": table, "name": name}).scalar()


def _event_type_check_def(conn):
    return _check_def(conn, "events", EVENT_TYPE_CHECK_NAME)


def _replace_check(conn, table, name, body):
    # Postgres can't ALTER a CHECK in place: drop and re-add it in the
    # caller's transaction, so no concurrent writer ever sees the table
    # unconstrained.
    conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
    conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({body})"))


def _column_exists(conn, table, column):
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t "
        "AND column_name = :c"), {"t": table, "c": column}).scalar())


def _run_migrations(conn=None):
    """Idempotent, additive migrations for columns added after the original
    schema.sql shipped. create_all() does NOT alter existing tables, so
    databases created before a column existed need these. Each statement is
    IF NOT EXISTS (or checks first) -- safe to run on every startup. (Whole
    new tables need nothing here: create_all() creates missing tables, with
    their indexes and constraints -- that is how existing databases get
    tasks (Phase 1) and job_scores (Phase 3).)

    Runs under the migration advisory lock, in the caller's transaction if
    one is passed (init_db does), else in its own."""
    if conn is None:
        with get_engine().begin() as own:
            _run_migrations(own)
        return
    _take_migration_lock(conn)
    conn.execute(text(
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS note TEXT"))
    # v2 corpus identity + change detection. The DEFAULT backfills
    # existing rows with an empty postings list.
    conn.execute(text(
        "ALTER TABLE canonical_jobs ADD COLUMN IF NOT EXISTS "
        "postings JSONB NOT NULL DEFAULT '[]'::jsonb"))
    conn.execute(text(
        "ALTER TABLE canonical_jobs ADD COLUMN IF NOT EXISTS content_hash TEXT"))
    conn.execute(text(
        "ALTER TABLE canonical_jobs ADD COLUMN IF NOT EXISTS "
        "content_changed_at TIMESTAMPTZ"))
    # v2 queue (Phase 2) columns on a Phase 1 tasks table. The partial
    # unique index allows one queued/running task per concurrency_key; all
    # pre-existing rows have a NULL key, so it builds without conflicts.
    for column in ("concurrency_key TEXT", "result JSONB",
                   "started_at TIMESTAMPTZ", "finished_at TIMESTAMPTZ"):
        conn.execute(text(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {column}"))
    conn.execute(text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {TASK_CONCURRENCY_INDEX} "
        f"ON tasks (concurrency_key) WHERE {_task_active_predicate()}"))
    # Phase 3 review: job_scores is keyed on the scoring fingerprint
    # (scoring.cache_key()), not the bare FORMULA_VERSION. The rename keeps
    # the primary key and lookup index (they follow the column); the old
    # rows' keys ("1") never match a fingerprinted key, so they are simply
    # rescored once.
    if _column_exists(conn, "job_scores", "formula_version") and \
            not _column_exists(conn, "job_scores", "cache_key"):
        conn.execute(text(
            "ALTER TABLE job_scores RENAME COLUMN formula_version TO cache_key"))
    # v2 agent event types. Postgres can't ALTER a CHECK in place, so a
    # stale constraint is dropped and re-added in this one transaction (no
    # concurrent writer ever sees the table unconstrained; the new set is a
    # superset, so existing rows validate). An up-to-date constraint is left
    # alone: re-running never rewrites it (same pg_constraint oid).
    # Phase 5: the posting triage matched on location (agent/locations.py).
    conn.execute(text(
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS posting JSONB"))
    _migrate_phase5(conn)
    _migrate_phase6(conn)
    if not event_type_check_is_current(_event_type_check_def(conn)):
        _replace_check(conn, "events", EVENT_TYPE_CHECK_NAME, event_type_check_sql())


def _migrate_phase6(conn):
    """Phase 6 on a Phase 5 database (each step checks first).
    agent_memories is a new table: create_all() made it."""
    # Which memories moved an application up the triage order.
    conn.execute(text(
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS memory_effects JSONB"))


def _migrate_phase5(conn):
    """Phase 5 (approval gate) on a Phase 4 database. Every step checks
    first, so re-running changes nothing. approvals and
    application_actions are new tables: create_all() made them."""
    # Who caused an agent event.
    conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT"))
    if _check_def(conn, "events", EVENT_SOURCE_CHECK_NAME) is None:
        conn.execute(text(
            f"ALTER TABLE events ADD CONSTRAINT {EVENT_SOURCE_CHECK_NAME} "
            f"CHECK ({_in_list('source', EVENT_SOURCES)})"))
    # The application's review flag and documents version.
    conn.execute(text(
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS "
        "needs_review BOOLEAN NOT NULL DEFAULT false"))
    conn.execute(text(
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS "
        "version INTEGER NOT NULL DEFAULT 0"))
    # The new status set. Phase 4's drafted / needs_review rows are
    # awaiting the user now: version 1 (their one draft), with the flag
    # carrying what 'needs_review' meant -- and the approval expiry task
    # every awaiting application gets.
    name = "applications_status_check"
    if not check_allows_exactly(_check_def(conn, "applications", name), "status",
                                APPLICATION_STATUSES):
        conn.execute(text(f"ALTER TABLE applications DROP CONSTRAINT IF EXISTS {name}"))
        migrated = conn.execute(text(
            "UPDATE applications SET needs_review = (status = 'needs_review'), "
            "status = 'awaiting_approval', version = 1, updated_at = now() "
            "WHERE status IN ('drafted', 'needs_review') RETURNING id")).scalars().all()
        conn.execute(text(f"ALTER TABLE applications ADD CONSTRAINT {name} "
                          f"CHECK ({_in_list('status', APPLICATION_STATUSES)})"))
        if migrated:
            from ..agent import store                 # lazy: agent imports db
            for application_id in migrated:
                store.enqueue_expiry(application_id, 1, conn=conn)
    # The packet artifact.
    name = "application_artifacts_kind_check"
    if not check_allows_exactly(_check_def(conn, "application_artifacts", name),
                                "kind", ARTIFACT_KINDS):
        _replace_check(conn, "application_artifacts", name,
                       _in_list("kind", ARTIFACT_KINDS))
