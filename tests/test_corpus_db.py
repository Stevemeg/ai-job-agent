"""Corpus module against a real Postgres (throwaway schema per test)."""
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend.database import corpus               # noqa: E402

pytestmark = pytest.mark.db

SAMPLE_JOBS = Path(__file__).resolve().parents[1] / "data" / "sample_jobs.json"


def _job(title="AI Engineer", company="acme", desc="Build LLM systems.",
         location="Remote", postings=None):
    job = {"title": title, "company": company, "location": location,
           "tags": ["ml"], "experience": None, "seniority": "Mid-Level",
           "clean_description": desc, "apply_link": "https://x/apply",
           "source": "ats"}
    if postings is not None:
        job["postings"] = postings
    return job


def _posting(pid, location):
    return {"source": "greenhouse", "posting_id": pid, "location": location,
            "apply_link": f"https://boards.greenhouse.io/acme/jobs/{pid}"}


def _rows(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT id, company, normalized_title, postings, content_hash, "
            "content_changed_at, first_seen_at, last_seen_at "
            "FROM canonical_jobs ORDER BY id")).mappings().all()


# ---- REQUIRED: upsert semantics --------------------------------------------

def test_upsert_twice_same_rows_first_seen_kept_last_seen_bumped(pg):
    jobs = [_job("AI Engineer"), _job("Data Scientist"), _job("ML Engineer")]
    assert corpus.upsert_jobs(jobs) == {"inserted": 3, "updated": 0, "changed": 0}
    before = _rows(pg)
    time.sleep(0.01)

    assert corpus.upsert_jobs(jobs) == {"inserted": 0, "updated": 3, "changed": 0}
    after = _rows(pg)

    assert len(after) == len(before) == 3
    for b, a in zip(before, after):
        assert a["id"] == b["id"]
        assert a["first_seen_at"] == b["first_seen_at"]
        assert a["last_seen_at"] > b["last_seen_at"]


def test_same_title_two_locations_is_one_row_two_postings_no_dup_on_rerun(pg):
    jobs = [_job(location="New York", postings=[_posting("101", "New York")]),
            _job(location="London", postings=[_posting("102", "London")])]
    assert corpus.upsert_jobs(jobs) == {"inserted": 1, "updated": 0, "changed": 0}
    assert corpus.upsert_jobs(jobs) == {"inserted": 0, "updated": 1, "changed": 0}

    rows = _rows(pg)
    assert len(rows) == 1
    assert sorted(p["posting_id"] for p in rows[0]["postings"]) == ["101", "102"]

    # A later scan that only sees one of the postings keeps both (union).
    corpus.upsert_jobs(jobs[:1])
    assert len(_rows(pg)[0]["postings"]) == 2


def test_description_change_moves_hash_and_changed_at_identical_rerun_does_not(pg):
    corpus.upsert_jobs([_job(desc="v1 description")])
    first = _rows(pg)[0]
    time.sleep(0.01)

    assert corpus.upsert_jobs([_job(desc="v1   description")]) == \
        {"inserted": 0, "updated": 1, "changed": 0}        # whitespace only
    same = _rows(pg)[0]
    assert same["content_hash"] == first["content_hash"]
    assert same["content_changed_at"] == first["content_changed_at"]
    time.sleep(0.01)

    assert corpus.upsert_jobs([_job(desc="v2 description")]) == \
        {"inserted": 0, "updated": 1, "changed": 1}
    changed = _rows(pg)[0]
    assert changed["content_hash"] != first["content_hash"]
    assert changed["content_changed_at"] > first["content_changed_at"]
    assert changed["first_seen_at"] == first["first_seen_at"]


def test_upsert_completes_a_stub_row_without_breaking_its_events(pg):
    """v1 impression/outcome logging creates bare canonical_jobs rows; the
    first collector sighting must fill that row in place (same id), so
    existing events keep pointing at the right job."""
    from backend.database.db import init_db
    from backend.database.events import log_outcome
    from backend.database.tracker import get_or_create_user

    init_db()
    user_id = get_or_create_user("t@example.com")
    log_outcome(user_id, "acme", "AI Engineer", "applied")
    stub = _rows(pg)[0]
    assert stub["content_hash"] is None
    assert corpus.load_corpus_snapshot().source != "db"   # stubs aren't corpus

    assert corpus.upsert_jobs([_job()]) == {"inserted": 0, "updated": 1, "changed": 1}
    filled = _rows(pg)[0]
    assert filled["id"] == stub["id"]
    assert filled["first_seen_at"] == stub["first_seen_at"]
    assert filled["content_hash"] is not None
    with pg.connect() as conn:
        assert conn.execute(text("SELECT job_id FROM events")).scalar() == stub["id"]


# ---- load_corpus: DB first --------------------------------------------------

def test_load_corpus_prefers_db_over_json(pg, tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"title": "From JSON"}]), encoding="utf-8")
    corpus.upsert_jobs([_job("From DB")])

    snap = corpus.load_corpus_snapshot(jobs_file, tmp_path / "sample.json")
    assert snap.source == "db"
    assert [j["title"] for j in snap.jobs] == ["From DB"]
    assert set(snap.jobs[0]) == set(corpus.JOB_FIELDS) | {"postings"}


def test_load_corpus_falls_back_to_json_when_db_holds_no_corpus(pg, tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"title": "From JSON"}]), encoding="utf-8")
    snap = corpus.load_corpus_snapshot(jobs_file, tmp_path / "sample.json")
    assert (snap.source, snap.jobs) == ("jobs_file", [{"title": "From JSON"}])


def test_strict_mode_reads_the_db_corpus(pg):
    corpus.upsert_jobs([_job("From DB")])
    snap = corpus.load_corpus_snapshot(require_db=True)
    assert snap.source == "db"
    assert [j["title"] for j in snap.jobs] == ["From DB"]


def test_strict_mode_empty_db_raises_instead_of_using_json(pg, tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"title": "From JSON"}]), encoding="utf-8")
    with pytest.raises(corpus.CorpusUnavailable) as err:
        corpus.load_corpus_snapshot(jobs_file, tmp_path / "s.json", require_db=True)
    assert err.value.reason == "empty"


def test_strict_mode_recovers_once_the_db_is_reachable_again(pg, monkeypatch):
    """The probe goes through the env-built URL; pointing it at a closed
    port simulates an outage without touching the test schema's engine."""
    import socket
    corpus.upsert_jobs([_job("From DB")])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    with monkeypatch.context() as m:
        m.delenv("DATABASE_URL", raising=False)
        m.setenv("DB_HOST", "127.0.0.1")
        m.setenv("DB_PORT", str(closed))
        m.setenv("DB_CONNECT_TIMEOUT", "2")
        with pytest.raises(corpus.CorpusUnavailable) as err:
            corpus.load_corpus_snapshot(require_db=True)
        assert err.value.reason == "unreachable"
    assert corpus.load_corpus_snapshot(require_db=True).source == "db"


def test_db_snapshot_cache_refreshes_after_an_upsert(pg, tmp_path):
    corpus.upsert_jobs([_job("One")])
    first = corpus.load_corpus_snapshot()
    assert corpus.load_corpus_snapshot().jobs is first.jobs      # cache hit
    corpus.upsert_jobs([_job("Two")])
    second = corpus.load_corpus_snapshot()
    assert second.version != first.version
    assert [j["title"] for j in second.jobs] == ["One", "Two"]


def test_db_round_trip_feeds_the_matcher_identical_fields_in_order(pg):
    """'Ranking output for the same corpus must not change': rank_jobs is a
    pure function of these fields and their order, so equality here means
    a DB-sourced ranking scores and orders exactly like the JSON-sourced one.

    One deliberate normalization: company is stored stripped, because the
    stripped value IS the UNIQUE key (v1's events._get_or_create_job strips
    too). The matcher never scores on company, so only the display string of
    a company with stray edge whitespace can differ."""
    source = json.loads(SAMPLE_JOBS.read_text(encoding="utf-8"))
    corpus.upsert_jobs(source)
    loaded = corpus.load_corpus()
    assert len(loaded) == len(corpus.dedupe_jobs(source)) == len(source)
    for want, got in zip(source, loaded):
        for field in corpus.JOB_FIELDS:
            if field == "company":
                assert got[field] == want[field].strip()
            else:
                assert got[field] == want.get(field), field


# ---- export ----------------------------------------------------------------

def test_export_corpus_json_round_trips(pg, tmp_path):
    corpus.upsert_jobs([_job("A", postings=[_posting("1", "Remote")]), _job("B")])
    out = tmp_path / "jobs.json"
    assert corpus.export_corpus_json(out) == 2
    assert json.loads(out.read_text(encoding="utf-8")) == corpus.load_corpus()


def test_export_refuses_to_write_an_empty_corpus(pg, tmp_path):
    out = tmp_path / "jobs.json"
    out.write_text("[1]", encoding="utf-8")
    with pytest.raises(RuntimeError):
        corpus.export_corpus_json(out)
    assert out.read_text(encoding="utf-8") == "[1]"
