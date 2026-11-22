"""ATS collector: posting-id capture, the JSON-only deduped merge, and where
a collection run is persisted. No network: _get_with_retries is stubbed with
payloads shaped like each public board API."""
import json

import pytest

pytest.importorskip("requests")
pytest.importorskip("bs4")                         # job_parser cleans HTML

from backend.job_scraper import ats_collector      # noqa: E402

GREENHOUSE = {"jobs": [
    {"id": 4001, "internal_job_id": 77, "title": "ML Engineer",
     "location": {"name": "New York"}, "departments": [{"name": "AI"}],
     "content": "<p>Train models.</p>",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001"},
    {"id": 4002, "internal_job_id": 77, "title": "ML Engineer",
     "location": {"name": "London"}, "departments": [{"name": "AI"}],
     "content": "<p>Train models.</p>",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/4002"},
]}
LEVER = [{"id": "lv-uuid-1", "text": "Data Engineer",
          "categories": {"location": "Remote", "team": "Data"},
          "descriptionPlain": "Pipelines.", "hostedUrl": "https://jobs.lever.co/x/1"}]
ASHBY = {"jobs": [{"id": "ash-uuid-1", "title": "AI Engineer", "location": "SF",
                   "department": "Eng", "descriptionPlain": "LLMs.",
                   "jobUrl": "https://jobs.ashbyhq.com/y/1"}]}


@pytest.fixture
def stub_boards(monkeypatch):
    def fake_get(url, params=None):
        if "greenhouse" in url:
            return GREENHOUSE
        if "lever" in url:
            return LEVER
        return ASHBY
    monkeypatch.setattr(ats_collector, "_get_with_retries", fake_get)


@pytest.mark.parametrize("fetch, board, ats, ids", [
    ("fetch_greenhouse_jobs", "acme", "greenhouse", ["4001", "4002"]),
    ("fetch_lever_jobs", "x", "lever", ["lv-uuid-1"]),
    ("fetch_ashby_jobs", "y", "ashby", ["ash-uuid-1"]),
])
def test_collectors_capture_the_ats_posting_id(stub_boards, fetch, board, ats, ids):
    jobs = getattr(ats_collector, fetch)(board)
    assert [p["posting_id"] for j in jobs for p in j["postings"]] == ids
    for job in jobs:
        (posting,) = job["postings"]
        assert posting["source"] == ats
        assert posting["location"] == job["location"]
        assert posting["apply_link"] == job["apply_link"]
        assert job["source"] == "ats"                # row-level provenance unchanged


def test_normalize_shape_is_a_superset_of_the_v1_shape():
    job = ats_collector._normalize("T", "c", "L", [], "<p>d</p>", "u")
    assert set(job) == {"title", "company", "location", "tags", "experience",
                        "seniority", "clean_description", "apply_link",
                        "source", "postings"}
    assert job["postings"] == []                     # no id -> no posting


# ---- REQUIRED: JSON-only merge dedupes -------------------------------------

def _j(title, company="acme", desc="d", pid=None, location="Remote"):
    job = {"title": title, "company": company, "location": location, "tags": [],
           "clean_description": desc, "apply_link": None, "source": "ats"}
    if pid is not None:
        job["postings"] = [{"source": "greenhouse", "posting_id": pid,
                            "location": location, "apply_link": None}]
    return job


def test_json_merge_dedupes_instead_of_appending(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    remoteok = {"title": "Backend Dev", "company": "other", "tags": [],
                "clean_description": "x", "location": "Remote", "apply_link": None}
    # an existing file already damaged by the old append behaviour
    existing = [_j("ML Engineer", desc="old"), remoteok, _j("ml  engineer", desc="old")]
    jobs_path.write_text(json.dumps(existing), encoding="utf-8")

    new = [_j("ML Engineer", desc="new", pid="1", location="NYC"),
           _j("ML Engineer", desc="new", pid="2", location="London"),
           _j("AI Engineer")]
    stats = ats_collector.merge_into_jobs_file(new, jobs_path)
    first = json.loads(jobs_path.read_text(encoding="utf-8"))

    assert stats == {"total": 3, "added": 1, "updated": 1}
    assert [j["title"] for j in first] == ["ML Engineer", "Backend Dev", "AI Engineer"]
    assert first[0]["clean_description"] == "new"              # latest wins
    assert [p["posting_id"] for p in first[0]["postings"]] == ["1", "2"]
    assert first[1] == remoteok                                 # other sources kept

    # re-running the same collection changes nothing
    ats_collector.merge_into_jobs_file(new, jobs_path)
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == first


def test_json_merge_creates_the_file_when_absent(tmp_path):
    jobs_path = tmp_path / "data" / "jobs.json"
    ats_collector.merge_into_jobs_file([_j("A"), _j("A")], jobs_path)
    assert len(json.loads(jobs_path.read_text(encoding="utf-8"))) == 1


# ---- where a run is persisted ----------------------------------------------

def test_save_without_a_database_writes_deduped_json(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    monkeypatch.setattr(ats_collector, "JOBS_FILE", jobs_path)
    ats_collector.save_collected_jobs([_j("A"), _j("B")])
    ats_collector.save_collected_jobs([_j("A"), _j("B")])
    assert [j["title"] for j in json.loads(jobs_path.read_text(encoding="utf-8"))] \
        == ["A", "B"]


@pytest.mark.db
def test_save_with_a_database_upserts_and_leaves_json_alone(pg, tmp_path,
                                                            monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    monkeypatch.setattr(ats_collector, "JOBS_FILE", jobs_path)
    first = ats_collector.save_collected_jobs([_j("A", pid="1"), _j("B")])
    again = ats_collector.save_collected_jobs([_j("A", pid="1"), _j("B")])
    assert first == {"inserted": 2, "updated": 0, "changed": 0}
    assert again == {"inserted": 0, "updated": 2, "changed": 0}
    assert not jobs_path.exists()
    from sqlalchemy import text
    with pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM canonical_jobs")).scalar() == 2
