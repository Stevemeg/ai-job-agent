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


def test_start_ranking_without_a_db_is_503(client):
    """No database configured (conftest strips it): there is no queue to
    run on, so POST refuses clearly instead of ranking in-process."""
    c, tmp = client
    r = c.post("/v1/rankings")
    assert r.status_code == 503
    assert "DATABASE_URL" in r.json()["detail"]
    assert not (tmp / "ranked_jobs.json").exists()


def test_ranking_status_without_a_db_or_previous_run_is_idle(client):
    c, _ = client
    assert c.get("/v1/rankings/status").json() == {
        "state": "idle", "detail": "", "ranked_count": None}


# ---- /v1/scans without a (reachable) database ---------------------------------------

def test_scans_need_a_database(client):
    c, _ = client
    for method, path in (("post", "/v1/scans"), ("get", "/v1/scans/latest")):
        r = getattr(c, method)(path)
        assert r.status_code == 503
        assert "DATABASE_URL" in r.json()["detail"]


def test_scan_post_with_an_unreachable_database_is_503(client, monkeypatch):
    pytest.importorskip("psycopg2")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", str(closed))
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "1")
    c, _ = client
    r = c.post("/v1/scans")
    assert r.status_code == 503
    assert "Database unavailable" in r.json()["detail"]


# ---- /v1/agent and /v1/applications without a database -------------------------------

def test_the_agent_endpoints_need_a_database(client):
    c, _ = client
    for method, path in (("post", "/v1/agent/runs"), ("get", "/v1/agent/runs/latest"),
                         ("get", "/v1/applications"), ("get", "/v1/applications/1"),
                         ("get", "/v1/applications/1/artifacts/resume_docx")):
        r = getattr(c, method)(path)
        assert r.status_code == 503, path
        assert "DATABASE_URL" in r.json()["detail"]


def test_the_decision_endpoint_needs_a_database(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    c, _ = client
    r = c.post("/v1/applications/1/decision", json={"action": "approve", "version": 1})
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]


# ---- API_TOKEN ------------------------------------------------------------------------

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def test_every_mutating_route_requires_the_token():
    from fastapi.routing import APIRoute
    from backend.api.auth import require_token
    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    routes = [r for r in api_routes if r.methods & MUTATING]
    assert {r.path for r in routes} >= {
        "/v1/resumes", "/v1/profile", "/v1/rankings", "/v1/scans", "/v1/tracker/events",
        "/v1/agent/runs", "/v1/applications/{application_id}/decision"}
    for r in routes:
        assert any(d.call is require_token for d in r.dependant.dependencies), r.path
    reads = [r for r in api_routes if not (r.methods & MUTATING)]
    assert all(not any(d.call is require_token for d in r.dependant.dependencies)
               for r in reads)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "s3cret"},
                                     {"Authorization": "Basic s3cret"}])
def test_with_api_token_set_a_missing_or_wrong_token_is_401(client, monkeypatch, headers):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    c, _ = client
    for method, path, body in (
            ("post", "/v1/applications/1/decision", {"action": "approve", "version": 1}),
            ("post", "/v1/agent/runs", None), ("post", "/v1/rankings", None),
            ("post", "/v1/scans", None), ("put", "/v1/profile", {})):
        r = getattr(c, method)(path, json=body, headers=headers)
        assert r.status_code == 401, (path, r.status_code)
        assert r.headers["www-authenticate"] == "Bearer"


def test_with_the_right_token_the_request_goes_through(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    c, _ = client
    r = c.post("/v1/applications/1/decision", json={"action": "approve", "version": 1},
               headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 503                      # past auth; no database here
    assert c.get("/healthz").status_code == 200      # reads need no token
    assert c.get("/v1/profile").status_code == 200


def test_without_api_token_nothing_is_checked(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    c, _ = client
    r = c.post("/v1/applications/1/decision", json={"action": "approve", "version": 1})
    assert r.status_code == 503

