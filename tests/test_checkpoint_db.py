"""LangGraph's Postgres checkpointer: set up under the migration lock in
the app's schema, and -- with durability="sync" -- a graph that fails
part-way resumes at the failed node with everything before it kept."""
import threading
from typing import TypedDict

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend.agent import checkpoint              # noqa: E402
from backend.database import db                   # noqa: E402

pytestmark = pytest.mark.db


def _tables(pg):
    with pg.connect() as conn:
        return set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )).scalars())


def test_setup_creates_langgraphs_tables_in_the_apps_schema(agent_db):
    with checkpoint.open_checkpointer():
        pass
    assert set(checkpoint.CHECKPOINT_TABLES) <= _tables(agent_db)
    assert {"applications", "application_artifacts", "tasks"} <= _tables(agent_db)
    checkpoint.reset()
    with checkpoint.open_checkpointer():          # setup() again: a no-op
        pass
    with agent_db.connect() as conn:
        versions = conn.execute(text(
            "SELECT count(*), count(DISTINCT v) FROM checkpoint_migrations")).one()
    assert versions[0] == versions[1] > 0          # each migration recorded once


def test_setup_waits_for_the_migration_lock(agent_db):
    db.init_db()
    holder = agent_db.connect()
    trans = holder.begin()
    holder.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": db.MIGRATION_LOCK_KEY})
    done, errors = threading.Event(), []

    def open_it():
        try:
            with checkpoint.open_checkpointer():
                pass
        except Exception as exc:                   # noqa: BLE001
            errors.append(exc)
        done.set()

    t = threading.Thread(target=open_it)
    t.start()
    try:
        assert not done.wait(1.5)                  # blocked behind init_db's lock
        assert "checkpoints" not in _tables(agent_db)
    finally:
        trans.rollback()
        holder.close()
    t.join(30)
    assert done.is_set() and not errors
    assert set(checkpoint.CHECKPOINT_TABLES) <= _tables(agent_db)


class S(TypedDict, total=False):
    log: list


def test_a_failed_step_resumes_with_the_earlier_steps_kept(agent_db):
    from langgraph.graph import END, START, StateGraph
    runs = {"a": 0, "b": 0, "c": 0}
    fail = {"c": True}

    def node(name):
        def run(state):
            runs[name] += 1
            if fail.get(name):
                raise RuntimeError(f"{name} died")
            return {"log": state.get("log", []) + [name]}
        return run

    g = StateGraph(S)
    for name in "abc":
        g.add_node(name, node(name))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    config = checkpoint.thread_config("test:1")
    with checkpoint.open_checkpointer() as saver:
        graph = g.compile(checkpointer=saver)
        with pytest.raises(RuntimeError):
            graph.invoke({"log": []}, config, durability=checkpoint.DURABILITY)
        state = graph.get_state(config)
        assert state.next == ("c",) and state.values["log"] == ["a", "b"]
    fail["c"] = False
    with checkpoint.open_checkpointer() as saver:     # a new connection: a retry
        graph = g.compile(checkpointer=saver)
        out = graph.invoke(None, config, durability=checkpoint.DURABILITY)
    assert out["log"] == ["a", "b", "c"]
    assert runs == {"a": 1, "b": 1, "c": 2}            # only the failed node re-ran
