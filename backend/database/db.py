"""
db.py — Postgres connection layer + ORM models for users, canonical_jobs,
events, and tasks.

Connection string is read from environment variables (see .env.example).
Never hardcode credentials here -- this file is committed to your repo.

Usage:
    from backend.db.db import get_session, User, CanonicalJob, Event

    with get_session() as session:
        user = session.query(User).filter_by(email="...").first()
"""
import os
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Text, ForeignKey,
    TIMESTAMP, Numeric, ARRAY, CheckConstraint, UniqueConstraint, Index,
    func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()


import os


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


def event_type_check_sql() -> str:
    """The CHECK body for events.event_type -- single source for the ORM
    constraint and the migration that rewrites it on existing databases."""
    return "event_type IN (%s)" % ",".join(f"'{t}'" for t in VALID_EVENT_TYPES)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(event_type_check_sql(), name=EVENT_TYPE_CHECK_NAME),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(Integer, ForeignKey("canonical_jobs.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String, nullable=False)
    match_score = Column(Numeric(5, 2))
    score_breakdown = Column(JSONB)
    note = Column(Text)          # free-text note (tracker UI); nullable
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="events")
    job = relationship("CanonicalJob", back_populates="events")


TASK_STATUSES = ("queued", "running", "succeeded", "failed", "dead")


class Task(Base):
    """Durable work queue row (ADR-001 §2). Schema only in Phase 1 -- the
    worker that claims rows (SELECT ... FOR UPDATE SKIP LOCKED) is Phase 2.

    updated_at is refreshed by the ORM on ORM updates; raw-SQL writers (the
    future claim/heartbeat queries) must set it explicitly."""
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN (%s)" % ",".join(f"'{s}'" for s in TASK_STATUSES),
            name="tasks_status_check"),
        Index("idx_tasks_status_run_after", "status", "run_after"),
    )

    id = Column(BigInteger, primary_key=True)            # BIGSERIAL
    kind = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(Text, nullable=False, server_default=text("'queued'"))
    idempotency_key = Column(Text, unique=True)          # NULLs never collide
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False, server_default=text("3"))
    run_after = Column(TIMESTAMP(timezone=True), nullable=False,
                       server_default=func.now())
    lease_expires_at = Column(TIMESTAMP(timezone=True))
    locked_by = Column(Text)
    last_error = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())


_initialized = False


def init_db():
    """Creates all tables if they don't already exist and applies additive
    migrations. Idempotent AND memoized per process -- callers (tracker UI,
    API routes) may invoke this on every request without paying CREATE/ALTER
    round-trips each time (audit fix)."""
    global _initialized
    if _initialized:
        return
    Base.metadata.create_all(get_engine())
    _run_migrations()
    _initialized = True


def _run_migrations():
    """Idempotent, additive migrations for columns added after the original
    schema.sql shipped. create_all() does NOT alter existing tables, so
    databases created before a column existed need these. Each statement is
    IF NOT EXISTS -- safe to run on every startup. (Whole new tables, like
    tasks, need nothing here: create_all() creates missing tables.)"""
    with get_engine().begin() as conn:
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
        # v2 agent event types. Postgres can't ALTER a CHECK in place, so
        # drop and re-add it; both run in this one transaction, so no
        # concurrent writer ever sees the table unconstrained. Re-running is
        # harmless (the new set is a superset, so existing rows validate).
        conn.execute(text(
            f"ALTER TABLE events DROP CONSTRAINT IF EXISTS {EVENT_TYPE_CHECK_NAME}"))
        conn.execute(text(
            f"ALTER TABLE events ADD CONSTRAINT {EVENT_TYPE_CHECK_NAME} "
            f"CHECK ({event_type_check_sql()})"))