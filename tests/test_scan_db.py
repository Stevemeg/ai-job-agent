"""The "scan" task against a real Postgres: collect -> upsert -> rank lock
-> score the delta -> assemble -> write. No network (the ATS fetchers are
replaced per board) and no torch (the embedding similarity is stubbed;
the rest of the matcher is real)."""
import json
import threading
import time

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("requests")
pytest.importorskip("bs4")

from sqlalchemy import create_engine, text        # noqa: E402
from sqlalchemy.pool import NullPool              # noqa: E402

from backend import taskqueue, version            # noqa: E402
from backend.database import corpus, db           # noqa: E402
from backend.job_scraper import ats_collector     # noqa: E402
from backend.matching_engine.matcher import rank_jobs  # noqa: E402
from backend.services import ranking, scan, scoring  # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db

RESULT_KEYS = {"inserted", "updated", "changed", "scored", "skipped_unchanged",
               "sources_ok", "sources_failed", "duration_s", "corpus_version",
               "profile_hash", "formula_version", "ranked_count"}


def _job(company, i, **overrides):
    job = {"title": f"AI Engineer {i}", "company": company, "location": "Remote",
           "tags": ["Engineering"], "experience": "1 years", "seniority": "Junior",
           "clean_description": f"{company} role {i}: RAG with FAISS and LLMs.",
           "apply_link": f"https://{company}/{i}", "source": "ats",
           "postings": [{"source": "greenhouse", "posting_id": f"{company}-{i}"}]}
    job.update(overrides)
    return job


class Boards(dict):
    """{(ats, slug): [jobs] | Exception}: what each ATS board returns (mutate
    it between scans). .scored lists every job the scans scored."""
    scored: list


@pytest.fixture
def boards(pg, rank_files, monkeypatch, stub_semantic):
    data = Boards({("greenhouse", "acme"): [_job("acme", i) for i in range(3)],
                   ("ashby", "beta"): [_job("beta", i) for i in range(2)]})

    def fetcher(ats):
        def fetch(slug, strict=False):
            answer = data[(ats, slug)]
            if isinstance(answer, Exception):
                raise answer
            return [dict(j) for j in answer]
        return fetch

    monkeypatch.setattr(ats_collector, "default_sources", lambda: list(data))
    for ats in ("greenhouse", "ashby"):
        monkeypatch.setitem(ats_collector.FETCHERS, ats, fetcher(ats))

    scored = []

    def counting(profile):
        inner = scoring.matcher_scorer(profile)

        def score(job):
            scored.append(f"{job['company']}/{job['title']}")
            return inner(job)
        return score
    monkeypatch.setattr(ranking, "_make_scorer", counting)
    data.scored = scored
    return data


def _scan():
    return scan.run_scan_task({})


def _ranked(rank_files):
    return json.loads(rank_files[1].read_text(encoding="utf-8"))


def _assert_parity(rank_files, profile):
    expected = rank_jobs(profile, corpus.load_corpus_snapshot(require_db=True).jobs)
    assert rank_files[1].read_text(encoding="utf-8") == json.dumps(
        expected, indent=4, ensure_ascii=False)


def _make_due(pg):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now()"))


# ---- the happy path and the incremental sequence ------------------------------------

def test_a_scan_collects_upserts_scores_and_publishes(boards, rank_files, profile):
    result = _scan()
    assert set(result) == RESULT_KEYS
    assert (result["inserted"], result["updated"], result["changed"]) == (5, 0, 0)
    assert (result["scored"], result["skipped_unchanged"]) == (5, 0)
    assert (result["sources_ok"], result["sources_failed"]) == (2, [])
    assert result["ranked_count"] == len(_ranked(rank_files)) == 5
    assert result["corpus_version"].startswith("db:5:")
    assert result["profile_hash"] == scoring.profile_hash(profile)
    assert result["formula_version"] == version.FORMULA_VERSION
    assert isinstance(result["duration_s"], float) and result["duration_s"] >= 0
    _assert_parity(rank_files, profile)


def test_rescans_score_only_what_is_new_or_changed(boards, rank_files, profile,
                                                   monkeypatch):
    assert _scan()["scored"] == 5
    del boards.scored[:]

    unchanged = _scan()                                     # unchanged re-scan
    assert (unchanged["scored"], unchanged["skipped_unchanged"]) == (0, 5)
    assert (unchanged["inserted"], unchanged["updated"], unchanged["changed"]) \
        == (0, 5, 0)
    assert boards.scored == []

    boards[("greenhouse", "acme")][1] = _job(                # one changed description
        "acme", 1, clean_description="Now: computer vision with OpenCV.")
    changed = _scan()
    assert (changed["scored"], changed["changed"]) == (1, 1)
    assert boards.scored == ["acme/AI Engineer 1"]

    boards[("ashby", "beta")].append(_job("beta", 9))       # one new job
    del boards.scored[:]
    new = _scan()
    assert (new["scored"], new["inserted"]) == (1, 1)
    assert boards.scored == ["beta/AI Engineer 9"]
    _assert_parity(rank_files, profile)

    edited = {**profile, "skills": profile["skills"][:-2]}  # profile change
    rank_files[0].write_text(json.dumps(edited), encoding="utf-8")
    full = _scan()
    assert (full["scored"], full["skipped_unchanged"]) == (6, 0)
    assert full["profile_hash"] == scoring.profile_hash(edited)
    _assert_parity(rank_files, edited)

    monkeypatch.setattr(version, "FORMULA_VERSION", "test-next")   # formula change
    bumped = _scan()
    assert (bumped["scored"], bumped["formula_version"]) == (6, "test-next")
    _assert_parity(rank_files, edited)


# ---- per-source isolation ------------------------------------------------------------

def test_one_failing_source_is_reported_and_the_scan_succeeds(boards, rank_files, pg):
    boards[("ashby", "beta")] = ats_collector.SourceFetchError("HTTP 503 (gave up "
                                                               "after 3 attempts)")
    task_id = scan.request_scan({"trigger": "api"})
    run = Worker(lease_s=30).run_once()
    assert (run.task_id, run.outcome) == (task_id, "succeeded")
    result = taskqueue.get_task(task_id)["result"]
    assert result["sources_ok"] == 1
    assert result["sources_failed"] == [{"source": "ashby", "slug": "beta",
                                         "error": "HTTP 503 (gave up after 3 attempts)"}]
    assert (result["inserted"], result["scored"]) == (3, 3)
    assert {j["company"] for j in _ranked(rank_files)} == {"acme"}


def test_when_every_source_fails_the_task_fails_and_is_retried(boards, rank_files, pg):
    boards[("greenhouse", "acme")] = ConnectionError("network unreachable")
    boards[("ashby", "beta")] = ats_collector.SourceFetchError("HTTP 404 (board not found)")
    task_id = scan.request_scan({"trigger": "api"})
    worker = Worker(lease_s=30)
    assert worker.run_once().outcome == "failed"
    task = taskqueue.get_task(task_id)
    assert (task["status"], task["attempts"]) == ("queued", 1)        # will retry
    assert task["last_error"].startswith("AllSourcesFailed: all 2 ATS sources failed")
    assert "greenhouse/acme: ConnectionError: network unreachable" in task["last_error"]
    db.init_db()
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM canonical_jobs")).scalar() == 0
    assert not rank_files[1].exists()

    boards[("greenhouse", "acme")] = [_job("acme", 0)]     # the network is back
    _make_due(pg)
    assert worker.run_once().outcome == "succeeded"
    task = taskqueue.get_task(task_id)
    assert (task["status"], task["attempts"]) == ("succeeded", 2)
    assert task["result"]["sources_ok"] == 1


# ---- retry safety and the rank-lock race ----------------------------------------------

def test_a_retry_after_a_failure_past_the_upsert_still_scores_the_new_jobs(
        boards, rank_files, pg, profile, monkeypatch):
    real_execute = ranking.execute_rank
    calls = []

    def dies_after_upsert(p, ctx=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("worker died after the upsert")
        return real_execute(p, ctx=ctx)

    monkeypatch.setattr(ranking, "execute_rank", dies_after_upsert)
    task_id = scan.request_scan({})
    worker = Worker(lease_s=30)
    assert worker.run_once().outcome == "failed"
    with pg.connect() as conn:                       # the upsert had committed
        assert conn.execute(text("SELECT count(*) FROM canonical_jobs")).scalar() == 5
        assert conn.execute(text("SELECT count(*) FROM job_scores")).scalar() == 0

    _make_due(pg)
    assert worker.run_once().outcome == "succeeded"
    result = taskqueue.get_task(task_id)["result"]
    # Nothing looks new to the upsert any more -- the delta comes from
    # job_scores, so the jobs are scored anyway.
    assert (result["inserted"], result["changed"]) == (0, 0)
    assert result["scored"] == 5
    assert len(_ranked(rank_files)) == 5
    _assert_parity(rank_files, profile)


def test_a_scan_waits_for_the_rank_lock_then_ranks_everything_saved_meanwhile(
        boards, rank_files, pg, profile):
    db.init_db()                                     # so the poll below can read
    holder_engine = create_engine(db._build_db_url(), poolclass=NullPool)
    holder = holder_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    assert holder.execute(text("SELECT pg_try_advisory_lock(:k)"),
                          {"k": ranking.RANK_LOCK_KEY}).scalar()   # a rank is running
    out = {}
    t = threading.Thread(target=lambda: out.update(result=_scan()))
    try:
        t.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:          # the scan's upsert commits...
            with pg.connect() as conn:
                if conn.execute(text("SELECT count(*) FROM canonical_jobs")).scalar() == 5:
                    break
            time.sleep(0.05)
        time.sleep(0.5)
        assert t.is_alive() and not rank_files[1].exists()   # ...then it waits
        # Meanwhile another collector saves a job (while "the other rank" runs).
        corpus.upsert_jobs([_job("gamma", 0)])
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"),
                       {"k": ranking.RANK_LOCK_KEY})
        holder.close()
        holder_engine.dispose()
    t.join(30)
    result = out["result"]
    assert result["scored"] == 6                     # the scan's 5 + gamma's
    assert result["corpus_version"].startswith("db:6:")
    assert "gamma" in {j["company"] for j in _ranked(rank_files)}
    _assert_parity(rank_files, profile)


# ---- edges ---------------------------------------------------------------------------

def test_without_a_profile_the_corpus_is_refreshed_and_ranking_skipped(
        boards, rank_files, pg):
    rank_files[0].unlink()
    result = _scan()
    assert result["inserted"] == 5
    assert (result["scored"], result["profile_hash"], result["ranked_count"]) == (
        0, None, None)
    assert "no candidate profile" in result["rank_skipped"]
    assert result["corpus_version"].startswith("db:5:")
    assert not rank_files[1].exists()


def test_scans_hold_their_own_concurrency_key(boards, pg):
    corpus.upsert_jobs([_job("acme", 0)])
    first = scan.request_scan({})
    with pytest.raises(taskqueue.ConcurrencyConflict):
        scan.request_scan({})
    ranking.request_ranking()                        # a rank is independent
    assert scan.latest_scan_task()["id"] == first
