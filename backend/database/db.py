"""
db.py — Postgres connection layer + ORM models for users, canonical_jobs,
and events.

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
    create_engine, Column, Integer, String, Text, ForeignKey,
    TIMESTAMP, Numeric, ARRAY, CheckConstraint, UniqueConstraint, func
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

    events = relationship("Event", back_populates="job")

    @staticmethod
    def normalize_title(title):
        return " ".join((title or "").lower().split())


VALID_EVENT_TYPES = (
    "impression", "click", "save", "dismiss",
    "applied", "interview", "rejected", "offer",
)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('impression','click','save','dismiss','applied','interview','rejected','offer')",
            name="events_event_type_check",
        ),
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
    IF NOT EXISTS -- safe to run on every startup."""
    from sqlalchemy import text
    with get_engine().begin() as conn:
        conn.execute(text(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS note TEXT"))