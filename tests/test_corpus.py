"""Corpus module, no database needed: identity, postings merge, change
detection, and the load_corpus fallback order (DB -> jobs.json -> sample)."""
import json
import socket

import pytest

from backend.database import corpus


def _job(title="AI Engineer", company="acme", desc="Build LLM systems.",
         location="Remote", postings=None, **extra):
    job = {"title": title, "company": company, "location": location,
           "tags": [], "clean_description": desc, "apply_link": None}
    if postings is not None:
        job["postings"] = postings
    job.update(extra)
    return job


def _posting(pid, location="Remote", source="greenhouse", link=None):
    return {"source": source, "posting_id": pid, "location": location,
            "apply_link": link}


# ---- identity ----------------------------------------------------------------

def test_normalize_title_matches_the_orm_unique_key():
    pytest.importorskip("sqlalchemy")
    from backend.database.db import CanonicalJob
    for title in ["AI Engineer", "  ai   ENGINEER ", "Sr.\tML  Eng", "", None]:
        assert corpus.normalize_title(title) == CanonicalJob.normalize_title(title)


def test_job_key_strips_company_and_normalizes_title():
    assert corpus.job_key({"company": " acme ", "title": "AI  ENGINEER"}) == \
        ("acme", "ai engineer")


# ---- postings ----------------------------------------------------------------

def test_merge_postings_dedupes_on_source_and_id_latest_wins():
    old = [_posting("1", "NYC"), _posting("2", "SF")]
    new = [_posting("2", "San Francisco"), _posting("1", "NYC", source="ashby")]
    merged = corpus.merge_postings(old, new)
    assert [(p["source"], p["posting_id"], p["location"]) for p in merged] == [
        ("greenhouse", "1", "NYC"),
        ("greenhouse", "2", "San Francisco"),     # latest value, original slot
        ("ashby", "1", "NYC"),                    # same id, other ATS: distinct
    ]


def test_merge_postings_drops_entries_without_an_id_and_stringifies_ids():
    merged = corpus.merge_postings([{"source": "lever", "posting_id": None}],
                                   [_posting(123)])
    assert merged == [_posting("123")]


# ---- change detection --------------------------------------------------------

def test_content_hash_ignores_cosmetic_changes():
    base = corpus.content_hash(_job(), [_posting("1", "NYC"), _posting("2", "SF")])
    assert base == corpus.content_hash(
        _job(title="ai  ENGINEER", desc="Build   LLM\nsystems.",
             apply_link="https://elsewhere", tags=["x"]),
        [_posting("2", "sf "), _posting("1", "nyc")])       # order + case


@pytest.mark.parametrize("change", [
    {"desc": "Build LLM systems at scale."},
    {"title": "Senior AI Engineer"},
    {"location": "London"},
])
def test_content_hash_moves_on_real_changes(change):
    assert corpus.content_hash(_job(**change), []) != corpus.content_hash(_job(), [])


def test_content_hash_moves_when_the_location_set_grows():
    one = corpus.content_hash(_job(), [_posting("1", "NYC")])
    two = corpus.content_hash(_job(), [_posting("1", "NYC"), _posting("2", "SF")])
    assert one != two


# ---- JSON-path dedupe (same rules as the DB upsert) -------------------------

def test_dedupe_keeps_first_position_latest_fields_merged_postings():
    jobs = [
        _job("AI Engineer", desc="old", postings=[_posting("1", "NYC")]),
        _job("Data Scientist"),
        _job("ai   engineer", company=" acme", desc="new",
             postings=[_posting("2", "SF")]),
    ]
    out = corpus.dedupe_jobs(jobs)
    assert [j["title"] for j in out] == ["ai   engineer", "Data Scientist"]
    assert out[0]["clean_description"] == "new"
    assert [p["posting_id"] for p in out[0]["postings"]] == ["1", "2"]


def test_dedupe_is_a_no_op_on_an_already_unique_corpus():
    jobs = [_job(f"Role {i}") for i in range(5)]
    assert corpus.dedupe_jobs(jobs) == jobs           # no "postings" key added


# ---- load_corpus fallback order ---------------------------------------------

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]                     # released on close


@pytest.fixture
def corpus_files(tmp_path):
    jobs_file, sample_file = tmp_path / "jobs.json", tmp_path / "sample_jobs.json"
    jobs_file.write_text(json.dumps([{"title": "Real"}]), encoding="utf-8")
    sample_file.write_text(json.dumps([{"title": "Sample"}]), encoding="utf-8")
    return jobs_file, sample_file


def test_unconfigured_db_is_never_probed(corpus_files, monkeypatch):
    def _no_probe():
        raise AssertionError("probed a DB that was never configured")
    monkeypatch.setattr(corpus, "_probe", _no_probe)
    assert corpus.load_corpus(*corpus_files) == [{"title": "Real"}]
    assert corpus.db_available() is False


def test_fallback_order_db_unreachable_then_jobs_then_sample(corpus_files,
                                                             monkeypatch):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")
    monkeypatch.setenv("DB_HOST", "127.0.0.1")       # configured ...
    monkeypatch.setenv("DB_PORT", str(_closed_port()))  # ... but unreachable
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "2")
    probes = []
    real_probe = corpus._probe
    monkeypatch.setattr(corpus, "_probe",
                        lambda: probes.append(1) or real_probe())
    jobs_file, sample_file = corpus_files

    snap = corpus.load_corpus_snapshot(jobs_file, sample_file)
    assert (snap.source, snap.jobs) == ("jobs_file", [{"title": "Real"}])

    jobs_file.unlink()
    snap = corpus.load_corpus_snapshot(jobs_file, sample_file)
    assert (snap.source, snap.jobs) == ("sample", [{"title": "Sample"}])

    sample_file.unlink()
    snap = corpus.load_corpus_snapshot(jobs_file, sample_file)
    assert (snap.source, snap.jobs) == ("none", [])

    assert probes == [1]                     # reachability cached per process


def test_json_path_is_cached_on_mtime(corpus_files):
    import os
    jobs_file, sample_file = corpus_files
    first = corpus.load_corpus(jobs_file, sample_file)
    assert corpus.load_corpus(jobs_file, sample_file) is first      # cache hit

    jobs_file.write_text(json.dumps([{"title": "Refreshed"}]), encoding="utf-8")
    st = jobs_file.stat()
    os.utime(jobs_file, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert corpus.load_corpus(jobs_file, sample_file) == [{"title": "Refreshed"}]


def test_write_json_atomic_leaves_no_temp_file(tmp_path):
    target = tmp_path / "nested" / "jobs.json"
    corpus.write_json_atomic(target, [{"title": "X"}])
    assert json.loads(target.read_text(encoding="utf-8")) == [{"title": "X"}]
    assert [p.name for p in target.parent.iterdir()] == ["jobs.json"]
