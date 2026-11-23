"""
checkpoint.py -- LangGraph's Postgres checkpointer (ADR-001 §1).

PostgresSaver (langgraph-checkpoint-postgres) speaks psycopg 3, so it gets
its own connection, built from the same DATABASE_URL / DB_* settings as
the app's SQLAlchemy engine (psycopg2). libpq honours PGOPTIONS for both,
which is how tests point them at one throwaway schema.

setup() creates and migrates LangGraph's own tables (checkpoints,
checkpoint_blobs, checkpoint_writes, checkpoint_migrations; not in
schema.sql). It runs once per process, under the app's migration advisory
lock (db.MIGRATION_LOCK_KEY), so it never interleaves with another
process's init_db() or setup(). The lock is SESSION-level, on the saver's
own autocommit connection: setup() builds its indexes CONCURRENTLY, which
cannot run inside a transaction and waits for every open transaction to
finish -- holding the lock in a SQLAlchemy transaction instead would
deadlock against it.

Graphs run with durability="sync" (DURABILITY): each step's checkpoint is
committed before the next step starts, so a worker killed mid-node resumes
from the node it was in, with everything before it already saved.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

# LangGraph's strongest durability mode: "Changes are persisted
# synchronously before the next step starts."
DURABILITY = "sync"
CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs",
                     "checkpoint_writes")
CONNECT_TIMEOUT_S = 10

_setup_lock = threading.Lock()
_setup_done = False


def conninfo() -> str:
    """A libpq URI for the app's database (psycopg 3 accepts it)."""
    from sqlalchemy.engine import make_url
    from ..database.db import _build_db_url
    url = make_url(_build_db_url()).set(drivername="postgresql")
    if "connect_timeout" not in url.query:
        url = url.update_query_dict({"connect_timeout": str(CONNECT_TIMEOUT_S)})
    return url.render_as_string(hide_password=False)


def _setup(saver) -> None:
    from ..database.db import MIGRATION_LOCK_KEY
    with saver.conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            saver.setup()
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))


def reset() -> None:
    """Forget that setup() ran (tests: a fresh schema per test)."""
    global _setup_done
    with _setup_lock:
        _setup_done = False


@contextmanager
def open_checkpointer() -> Iterator:
    """A PostgresSaver on a fresh connection, closed on exit. The app's
    tables (init_db) and LangGraph's (setup, under the migration lock) are
    ensured first, once per process."""
    global _setup_done
    from langgraph.checkpoint.postgres import PostgresSaver
    from ..database.db import init_db

    init_db()
    with PostgresSaver.from_conn_string(conninfo()) as saver:
        with _setup_lock:
            if not _setup_done:
                _setup(saver)
                _setup_done = True
        yield saver


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}
