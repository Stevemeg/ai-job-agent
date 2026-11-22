"""Incremental scoring against a real Postgres: only the delta is scored,
and the assembled list is always exactly rank_jobs(profile, corpus).
The matcher is real; only the embedding similarity is stubbed."""
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend import version                       # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.matching_engine.matcher import rank_jobs  # noqa: E402
from backend.services import scoring              # noqa: E402

pytestmark = pytest.mark.db

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_jobs.json"


def _job(i, **overrides):
    job = {"title": f"AI Engineer {i}", "company": "acme", "location": "Remote",
           "tags": ["Engineering"], "experience": "1 years", "seniority": "Junior",
           "clean_description": f"Role {i}: build RAG pipelines with FAISS, "
                                "LLMs and Pandas. Git required.",
           "apply_link": f"https://x/{i}", "source": "ats"}
    job.update(overrides)
    return job


class Counter:
    """make_scorer for rank_incrementally: the real matcher scorer, recording
    every job it is asked to score."""
    def __init__(self):
        self.titles = []

    def __call__(self, profile):
        inner = scoring.matcher_scorer(profile)

        def score(job):
            self.titles.append(job["title"])
            return inner(job)
        return score


def _run(profile, counter=None, **kwargs):
    counter = counter or Counter()
    outcome = scoring.rank_incrementally(corpus.load_corpus_rows().rows, profile,
                                         make_scorer=counter, **kwargs)
    return outcome, counter.titles


def _full(profile):
    jobs = corpus.load_corpus_snapshot(require_db=True).jobs
    return rank_jobs(profile, jobs, candidate_is_fresher=True)


def _assert_parity(outcome, profile):
    # json text, not just ==: same jobs, order, values AND key order
    assert json.dumps(outcome.ranked) == json.dumps(_full(profile))


def test_parity_with_rank_jobs_on_the_sample_corpus(pg, profile, stub_semantic):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    corpus.upsert_jobs(sample)
    rows, snap = corpus.load_corpus_rows(), corpus.load_corpus_snapshot(require_db=True)
    assert (rows.jobs, rows.version) == (snap.jobs, snap.version)   # same corpus

    first, scored = _run(profile)
    assert first.scored == len(scored) == len(sample) and first.skipped_unchanged == 0
    assert 0 < len(first.ranked) < len(sample)            # some jobs gated out
    _assert_parity(first, profile)

    second, scored = _run(profile)                        # everything cached
    assert (second.scored, second.skipped_unchanged, scored) == (0, len(sample), [])
    assert second.ranked == first.ranked
    _assert_parity(second, profile)


def test_only_the_delta_is_rescored(pg, profile, stub_semantic, monkeypatch):
    jobs = [_job(i) for i in range(6)]
    jobs.append(_job(6, title="Sales Engineer"))           # gated out
    jobs.append(_job(7, company="ACME", title="AI Engineer 0"))   # matcher dup of 0
    corpus.upsert_jobs(jobs)

    outcome, scored = _run(profile)
    assert sorted(scored) == sorted(f"AI Engineer {i}" for i in range(6)) + [
        "Sales Engineer"]                       # 7 first-seen; the dup never
    assert outcome.skipped_unchanged == 0
    _assert_parity(outcome, profile)

    # unchanged re-scan
    corpus.upsert_jobs(jobs)
    outcome, scored = _run(profile)
    assert (outcome.scored, scored, outcome.skipped_unchanged) == (0, [], 7)
    _assert_parity(outcome, profile)

    # one changed description
    corpus.upsert_jobs([_job(2, clean_description="Now a computer vision role "
                                                  "with OpenCV and CLIP.")])
    outcome, scored = _run(profile)
    assert scored == ["AI Engineer 2"] and outcome.skipped_unchanged == 6
    _assert_parity(outcome, profile)

    # one new job
    corpus.upsert_jobs([_job(8)])
    outcome, scored = _run(profile)
    assert scored == ["AI Engineer 8"]
    _assert_parity(outcome, profile)

    # tags only: canonical content_hash doesn't move, the score input does
    with pg.connect() as conn:
        before = conn.execute(text("SELECT content_hash FROM canonical_jobs "
                                   "WHERE title = 'AI Engineer 3'")).scalar()
    corpus.upsert_jobs([_job(3, tags=["Research", "Machine Learning"])])
    with pg.connect() as conn:
        assert conn.execute(text("SELECT content_hash FROM canonical_jobs "
                                 "WHERE title = 'AI Engineer 3'")).scalar() == before
    outcome, scored = _run(profile)
    assert scored == ["AI Engineer 3"]
    _assert_parity(outcome, profile)

    # display-only change: nothing rescored, but the file shows the new link
    corpus.upsert_jobs([_job(4, apply_link="https://x/moved")])
    outcome, scored = _run(profile)
    assert scored == []
    assert {r["title"]: r["apply_link"] for r in outcome.ranked}[
        "AI Engineer 4"] == "https://x/moved"
    _assert_parity(outcome, profile)

    # profile change -> everything first-seen is rescored
    edited = {**profile, "skills": profile["skills"] + [
        {"skill": "PyTorch", "category": "Deep Learning", "domain": "AI"}]}
    outcome, scored = _run(edited)
    assert len(scored) == outcome.scored == 8 and outcome.skipped_unchanged == 0
    assert outcome.profile_hash != scoring.profile_hash(profile)
    _assert_parity(outcome, edited)
    # ...and the old profile's rows are still there to come back to
    outcome, scored = _run(profile)
    assert scored == []

    # FORMULA_VERSION change -> full rescore
    monkeypatch.setattr(version, "FORMULA_VERSION", "test-next")
    outcome, scored = _run(profile)
    assert len(scored) == 8 and outcome.formula_version == "test-next"
    _assert_parity(outcome, profile)


def test_gate_failures_are_stored_and_never_reevaluated(pg, profile, stub_semantic):
    corpus.upsert_jobs([_job(0), _job(1, title="Sales Engineer")])
    outcome, _ = _run(profile)
    assert [r["title"] for r in outcome.ranked] == ["AI Engineer 0"]
    with pg.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.title, s.passed, s.match_score, s.score_breakdown, "
            "s.explanation, s.profile_hash, s.formula_version FROM job_scores s "
            "JOIN canonical_jobs c ON c.id = s.job_id ORDER BY c.title")).all()
    assert [(r.title, r.passed) for r in rows] == [("AI Engineer 0", True),
                                                  ("Sales Engineer", False)]
    gated = rows[1]
    assert (gated.match_score, gated.score_breakdown, gated.explanation) == (
        None, None, None)                                  # SQL NULLs
    assert gated.profile_hash == scoring.profile_hash(profile)
    assert gated.formula_version == version.FORMULA_VERSION
    _, scored = _run(profile)
    assert scored == []


def test_a_run_that_dies_part_way_keeps_its_finished_batches(pg, profile,
                                                            stub_semantic):
    corpus.upsert_jobs([_job(i) for i in range(5)])

    def dies_on_the_fourth(profile):
        inner, calls = scoring.matcher_scorer(profile), []

        def score(job):
            calls.append(job["title"])
            if len(calls) == 4:
                raise RuntimeError("worker killed")
            return inner(job)
        return score

    with pytest.raises(RuntimeError):
        scoring.rank_incrementally(corpus.load_corpus_rows().rows, profile,
                                   make_scorer=dies_on_the_fourth, batch=2)
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM job_scores")).scalar() == 2
    outcome, scored = _run(profile)                 # the retry
    assert scored == ["AI Engineer 2", "AI Engineer 3", "AI Engineer 4"]
    _assert_parity(outcome, profile)


def test_a_worker_that_lost_its_lease_stops_scoring(pg, profile, stub_semantic):
    from backend.taskqueue import ClaimedTask
    from backend.worker import LeaseLost, TaskContext
    corpus.upsert_jobs([_job(i) for i in range(3)])
    ctx = TaskContext(task=ClaimedTask(1, "rank", {}, 1, 3, "w", None),
                      worker_id="w", lease_lost=threading.Event())
    ctx.lease_lost.set()
    counter = Counter()
    with pytest.raises(LeaseLost):
        _run(profile, counter, ctx=ctx)
    assert counter.titles == []
