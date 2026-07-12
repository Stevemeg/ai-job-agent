"""API contract tests. FastAPI is a light dependency; tests skip cleanly
where it isn't installed. All file paths are monkeypatched to tmp_path so
tests can never touch real user data."""
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from backend.api.app import app                    # noqa: E402
from backend.api import deps                       # noqa: E402
from backend.api.routers import rankings as rankings_router  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch, profile):
    """TestClient with all data paths redirected to tmp_path."""
    profile_file = tmp_path / "candidate_profile.json"
    jobs_file = tmp_path / "jobs.json"
    ranked_file = tmp_path / "ranked_jobs.json"

    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    jobs_file.write_text(json.dumps([
        {"title": "AI Engineer", "company": "TestCo",
         "clean_description": "LLM work. Docker required.", "tags": []},
    ] * 40), encoding="utf-8")

    monkeypatch.setattr(deps, "PROFILE_FILE", profile_file)
    monkeypatch.setattr(deps, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(deps, "RANKED_FILE", ranked_file)
    monkeypatch.setattr(rankings_router, "RANKED_FILE", ranked_file)

    return TestClient(app), tmp_path


def test_healthz_reports_formula_version(client):
    c, _ = client
    body = c.get("/healthz").json()
    assert body["status"] == "ok"
    assert "formula_version" in body


def test_get_profile_roundtrip(client, profile):
    c, _ = client
    r = c.get("/v1/profile")
    assert r.status_code == 200
    assert r.json()["name"] == profile["name"]


def test_profile_404_when_missing(client, monkeypatch, tmp_path):
    c, _ = client
    monkeypatch.setattr(deps, "PROFILE_FILE", tmp_path / "nope.json")
    assert c.get("/v1/profile").status_code == 404


def test_health_endpoint_matches_engine(client, profile):
    c, _ = client
    body = c.get("/v1/profile/health").json()
    from backend.analysis.resume_health import compute_health
    assert body["score"] == compute_health(profile)["score"]
    assert body["grade"] in "ABCDE"


def test_careers_and_gaps_endpoints(client):
    c, _ = client
    careers = c.get("/v1/profile/careers").json()
    assert careers and all(0 <= x["fit_pct"] <= 100 for x in careers)
    gaps = c.get("/v1/profile/gaps").json()
    assert any(g["skill"] == "Docker" for g in gaps)   # in fixture jobs
    for g in gaps:
        assert set(g) >= {"skill", "demand_pct", "role_demand_pct",
                          "role_weighted", "priority"}


def test_rankings_404_before_any_run(client):
    c, _ = client
    assert c.get("/v1/rankings").status_code == 404


def test_rankings_pagination_and_filter(client):
    c, tmp = client
    ranked = [{"title": f"Job {i}", "company": "X", "match_score": float(i),
               "score_breakdown": {"skill_overlap": 0.1, "semantic": 0.5,
                                   "role": 1.0, "seniority": 1.0},
               "strong_matches": [], "likely_matches": [],
               "missing_skills": [], "apply_link": None, "location": None}
              for i in range(60, 0, -1)]
    (tmp / "ranked_jobs.json").write_text(json.dumps(ranked), encoding="utf-8")

    r = c.get("/v1/rankings?limit=10").json()
    assert len(r) == 10 and r[0]["match_score"] == 60.0
    r2 = c.get("/v1/rankings?limit=10&offset=10").json()
    assert r2[0]["match_score"] == 50.0
    r3 = c.get("/v1/rankings?min_score=55").json()
    assert all(j["match_score"] >= 55 for j in r3) and len(r3) == 6


def test_ranking_status_reports_previous_run(client):
    c, tmp = client
    (tmp / "ranked_jobs.json").write_text("[]", encoding="utf-8")
    body = c.get("/v1/rankings/status").json()
    assert body["state"] in ("idle", "done")


def test_non_pdf_upload_rejected(client):
    c, _ = client
    r = c.post("/v1/resumes",
               files={"file": ("resume.txt", b"hello", "text/plain")})
    assert r.status_code == 422


def test_put_profile_saves_to_patched_path(client, monkeypatch, profile):
    pytest.importorskip("fitz")        # resume_parser.api imports PyMuPDF
    c, tmp = client
    from backend.resume_parser import api as parser_api
    target = tmp / "saved_profile.json"
    monkeypatch.setattr(parser_api, "PROFILE_FILE", target)

    profile["name"] = "Edited Name"
    r = c.put("/v1/profile", json=profile)
    assert r.status_code == 200
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "Edited Name"


def test_event_validation_rejects_bad_type(client):
    c, _ = client
    r = c.post("/v1/tracker/events", json={
        "company": "X", "title": "Y", "event_type": "ghosted"})
    assert r.status_code == 422        # schema rejects before touching the DB
