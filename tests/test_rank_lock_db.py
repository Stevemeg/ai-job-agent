"""The rank handler and the rank lock against a real Postgres: one rank
execution at a time (session-level advisory lock on a dedicated
connection), released if its holder dies, never left waiting forever. The
matcher is real; only the embedding similarity is stubbed."""
import json
import threading
import time

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text        # noqa: E402
from sqlalchemy.pool import NullPool              # noqa: E402

from backend.database import corpus, db           # noqa: E402
from backend.services import ranking, scoring     # noqa: E402
from backend.version import FORMULA_VERSION       # noqa: E402
from backend.worker import LeaseLost, TaskContext  # noqa: E402

pytestmark = pytest.mark.db


def _job(i):
    return {"title": f"AI Engineer {i}", "company": "acme", "location": "Remote",
            "tags": ["Engineering"], "seniority": "Junior",
            "clean_description": f"Role {i}: RAG with FAISS and LLMs.",
            "apply_link": f"https://x/{i}", "source": "ats"}


@pytest.fixture
def other_session():
    """A second database session (its own connection), e.g. to hold the
    rank lock the way another worker would."""
    engine = create_engine(db._build_db_url(), poolclass=NullPool)
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    yield conn
    conn.close()
    engine.dispose()


def _hold(conn):
    assert conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                        {"k": ranking.RANK_LOCK_KEY}).scalar()


def _release(conn):
    assert conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                        {"k": ranking.RANK_LOCK_KEY}).scalar()


def _lock_holder_pids(conn):
    return conn.execute(text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND objid::bigint = :k"), {"k": ranking.RANK_LOCK_KEY}).scalars().all()


def _ctx(lost=False):
    from backend.taskqueue import ClaimedTask
    ctx = TaskContext(task=ClaimedTask(1, "rank", {}, 1, 3, "w", None),
                      worker_id="w", lease_lost=threading.Event())
    if lost:
        ctx.lease_lost.set()
    return ctx


# ---- the "rank" handler ------------------------------------------------------------

def test_rank_handler_ranks_the_db_corpus_and_writes_atomically(
        pg, rank_files, profile, stub_semantic):
    _, ranked_file = rank_files
    corpus.upsert_jobs([_job(i) for i in range(3)])
    result = ranking.run_rank_task({})
    assert set(result) == {"ranked_count", "scored", "skipped_unchanged",
                           "corpus_version", "profile_hash", "formula_version"}
    assert (result["ranked_count"], result["scored"],
            result["skipped_unchanged"]) == (3, 3, 0)
    assert result["corpus_version"].startswith("db:3:")
    assert result["formula_version"] == FORMULA_VERSION
    assert result["profile_hash"] == scoring.profile_hash(profile)
    from backend.matching_engine.matcher import rank_jobs
    expected = rank_jobs(profile, corpus.load_corpus_snapshot(require_db=True).jobs)
    assert ranked_file.read_text(encoding="utf-8") == json.dumps(
        expected, indent=4, ensure_ascii=False)          # what rank_jobs would write
    assert [p.name for p in ranked_file.parent.iterdir()
            if p.name.endswith(".tmp")] == []


def test_rank_handler_is_safe_to_rerun(pg, rank_files, stub_semantic):
    _, ranked_file = rank_files
    corpus.upsert_jobs([_job(i) for i in range(3)])
    first = ranking.run_rank_task({})
    content = ranked_file.read_bytes()
    again = ranking.run_rank_task({})
    assert (again["scored"], again["skipped_unchanged"]) == (0, 3)
    assert {k: v for k, v in again.items() if k not in ("scored", "skipped_unchanged")} \
        == {k: v for k, v in first.items() if k not in ("scored", "skipped_unchanged")}
    assert ranked_file.read_bytes() == content


def test_rank_handler_never_writes_after_losing_its_lease(pg, rank_files,
                                                          stub_semantic):
    _, ranked_file = rank_files
    corpus.upsert_jobs([_job(0)])
    with pytest.raises(LeaseLost):
        ranking.run_rank_task({}, _ctx(lost=True))
    assert not ranked_file.exists()


# ---- the rank lock -----------------------------------------------------------------

def test_two_concurrent_rank_executions_serialize_on_the_lock(
        pg, rank_files, profile, monkeypatch, stub_semantic, other_session):
    _, ranked_file = rank_files
    corpus.upsert_jobs([_job(i) for i in range(4)])
    inside, peak, lock = [0], [0], threading.Lock()
    holders = []

    def slow_scorer(p):
        inner = scoring.matcher_scorer(p)
        with lock:                           # entered the locked section
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        holders.append(_lock_holder_pids(other_session))

        def score(job):
            time.sleep(0.15)
            return inner(job)
        return score

    real_write = ranking._write_ranked

    def write(ranked):
        real_write(ranked)
        with lock:
            inside[0] -= 1

    monkeypatch.setattr(ranking, "_make_scorer", slow_scorer)
    monkeypatch.setattr(ranking, "_write_ranked", write)
    results, errors = [], []

    def run():
        try:
            results.append(ranking.execute_rank(profile))
        except Exception as exc:                 # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    # One scored everything; the other waited, then found it all cached.
    assert sorted(r["scored"] for r in results) == [0, 4]
    assert peak[0] == 1                          # never both inside at once
    assert holders and all(len(pids) == 1 for pids in holders)   # lock was held
    assert json.loads(ranked_file.read_text(encoding="utf-8"))   # a whole file
    assert _lock_holder_pids(other_session) == []                # and released


def test_the_lock_is_released_when_its_holder_dies(pg, other_session):
    holding, release = threading.Event(), threading.Event()

    def holder():
        with ranking.rank_lock():
            holding.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert holding.wait(10)
    pids = _lock_holder_pids(other_session)
    assert len(pids) == 1
    # The worker process dies: its connection goes away with it.
    other_session.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pids[0]})
    started = time.monotonic()
    with ranking.rank_lock(wait_s=10):
        assert time.monotonic() - started < 5
    release.set()
    t.join(10)
    assert not t.is_alive()


def test_waiting_for_the_lock_gives_up_after_the_wait(pg, other_session):
    _hold(other_session)
    started = time.monotonic()
    with pytest.raises(ranking.RankLockTimeout):
        with ranking.rank_lock(wait_s=0.6):
            pytest.fail("acquired a lock someone else holds")
    assert time.monotonic() - started >= 0.6
    _release(other_session)


def test_a_waiter_that_lost_its_lease_stops_waiting(pg, other_session):
    _hold(other_session)
    with pytest.raises(LeaseLost):
        with ranking.rank_lock(ctx=_ctx(lost=True), wait_s=30):
            pytest.fail("acquired a lock someone else holds")
    _release(other_session)


def test_cli_publishing_holds_the_rank_lock_when_a_db_is_configured(
        pg, rank_files, monkeypatch, other_session):
    _, ranked_file = rank_files
    monkeypatch.setattr(ranking, "_rank_jobs", lambda p, jobs: [{"title": "A"}])
    _hold(other_session)
    done = threading.Event()
    t = threading.Thread(target=lambda: (ranking.rank_and_publish({}, []),
                                         done.set()))
    t.start()
    assert not done.wait(1.0)                 # blocked behind the worker's lock
    assert not ranked_file.exists()
    _release(other_session)
    t.join(10)
    assert done.is_set()
    assert json.loads(ranked_file.read_text(encoding="utf-8")) == [{"title": "A"}]
