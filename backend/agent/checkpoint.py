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

thread_lock(application_id) serializes the tasks that drive one
application's thread (its drafting task and its decision tasks): a
session-level Postgres advisory lock, held for the whole graph run.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

# LangGraph's strongest durability mode: "Changes are persisted
# synchronously before the next step starts."
DURABILITY = "sync"
CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs",
                     "checkpoint_writes")
CONNECT_TIMEOUT_S = 10
# pg_try_advisory_lock(class, application_id): b"appt".
APPLICATION_THREAD_LOCK_CLASS = 0x61707074
THREAD_LOCK_WAIT_S = 30.0

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


def run_thread(builder, thread_id: str, initial_state: dict) -> dict:
    """Run a StateGraph builder on `thread_id` to the end and return the
    final state -- resuming instead of restarting when the thread already
    has checkpoints:

      no checkpoint yet          -> invoke(initial_state)  (a first attempt)
      paused at an interrupt()   -> the saved state with its "__interrupt__"
                                    (it waits for Command(resume=...), which
                                    only a decision gives; nothing re-runs)
      checkpoint, steps pending  -> invoke(None): resume at the pending node
                                    (a retry after a crash or an error)
      checkpoint, graph finished -> the saved final state, nothing re-runs
                                    (the worker died between the graph's
                                    end and recording the task's success)

    A run that reaches an interrupt() returns its state with
    "__interrupt__" set, as LangGraph's invoke() does. Every step is
    checkpointed synchronously (DURABILITY)."""
    config = thread_config(thread_id)
    with open_checkpointer() as saver:
        graph = builder.compile(checkpointer=saver)
        if saver.get_tuple(config) is None:
            return graph.invoke(initial_state, config, durability=DURABILITY)
        snapshot = graph.get_state(config)
        if snapshot.interrupts:
            log.info("%s: paused at %s; waiting for a decision", thread_id,
                     ", ".join(snapshot.next))
            return {**snapshot.values, "__interrupt__": list(snapshot.interrupts)}
        if snapshot.next:
            log.info("%s: resuming at %s", thread_id, ", ".join(snapshot.next))
            return graph.invoke(None, config, durability=DURABILITY)
        log.info("%s: already finished; returning its final state", thread_id)
        return dict(snapshot.values)


class ThreadBusy(RuntimeError):
    """Another worker is driving this application's thread; the task is
    retried with backoff."""


@contextmanager
def thread_lock(application_id: int, wait_s: float = THREAD_LOCK_WAIT_S) -> Iterator[None]:
    """Hold the application's thread lock (a session-level advisory lock on
    its own connection) for the block; ThreadBusy if another task holds it
    for longer than wait_s. Unlocked before the connection goes back to
    the pool, and released by Postgres if this process dies."""
    from sqlalchemy import text
    from ..database.db import get_engine, init_db
    init_db()
    params = {"c": APPLICATION_THREAD_LOCK_CLASS, "id": int(application_id)}
    with get_engine().connect() as conn:
        deadline = time.monotonic() + wait_s
        while not conn.execute(text("SELECT pg_try_advisory_lock(:c, :id)"),
                               params).scalar():
            conn.rollback()
            if time.monotonic() >= deadline:
                raise ThreadBusy(f"application {application_id}: another task is "
                                 "running its graph")
            time.sleep(0.2)
        conn.commit()
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:c, :id)"), params)
            conn.commit()
