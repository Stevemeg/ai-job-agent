"""events._get_or_create_job against a real Postgres (throwaway schema)."""
import threading

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend.database.db import get_session, init_db   # noqa: E402
from backend.database.events import _get_or_create_job  # noqa: E402

pytestmark = pytest.mark.db

JOB = {"company": "acme", "title": "AI Engineer"}


def _count(pg):
    with pg.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM canonical_jobs")).scalar()


def test_concurrent_stub_creation_is_race_safe(pg):
    """Session 1 inserts the stub and holds its transaction open; session 2
    must wait on the conflicting insert, then return the SAME row once
    session 1 commits -- not fail with a unique violation."""
    init_db()
    results, errors = [], []

    def other_session():
        try:
            with get_session() as s2:
                results.append(_get_or_create_job(s2, JOB).id)
        except Exception as exc:                  # noqa: BLE001
            errors.append(exc)

    with get_session() as s1:
        first_id = _get_or_create_job(s1, JOB).id
        t = threading.Thread(target=other_session)
        t.start()
        t.join(1.0)
        assert t.is_alive(), "second session should block on the uncommitted stub"
    t.join(10)                                    # s1 committed on exit

    assert not errors, errors
    assert results == [first_id]
    assert _count(pg) == 1


def test_known_job_is_found_without_consuming_a_sequence_value(pg):
    init_db()
    with get_session() as s:
        first_id = _get_or_create_job(s, JOB).id
    seq_sql = text("SELECT last_value FROM "
                   "pg_sequences WHERE schemaname = current_schema() "
                   "AND sequencename = 'canonical_jobs_id_seq'")
    with pg.connect() as conn:
        before = conn.execute(seq_sql).scalar()
    with get_session() as s:
        again = _get_or_create_job(s, {"company": " acme ", "title": "ai  engineer"})
        assert again.id == first_id
    with pg.connect() as conn:
        assert conn.execute(seq_sql).scalar() == before
    assert _count(pg) == 1
