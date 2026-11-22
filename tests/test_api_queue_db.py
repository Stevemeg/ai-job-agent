"""/v1/rankings on the task queue, end to end against a real Postgres:
POST enqueues, the worker runs the task, /status maps it. rank_jobs is
stubbed (no torch in CI); everything else is the production path."""
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402

from backend import taskqueue                      # noqa: E402
from backend.api import deps                       # noqa: E402
from backend.api.app import app                    # noqa: E402
from backend.api.routers import rankings as rankings_router  # noqa: E402
from backend.database import corpus                # noqa: E402
from backend.services import ranking               # noqa: E402
from backend.version import FORMULA_VERSION        # noqa: E402
from backend.worker import Worker                  # noqa: E402

pytestmark = pytest.mark.db


def _job(title):
    return {"title": title, "company": "acme", "location": "Remote",
            "tags": ["ml"], "clean_description": f"{title}: build LLM systems.",
            "apply_link": "https://x/apply", "source": "ats"}


def _fake_rank(profile, jobs):
    return [{"title": j["title"], "company": j["company"],
             "location": j.get("location"), "match_score": 90.0 - i,
             "score_breakdown": {"skill_overlap": 0.5, "semantic": 0.5,
                                 "role": 1.0, "seniority": 1.0},
             "strong_matches": [], "likely_matches": [], "missing_skills": [],
             "apply_link": j.get("apply_link")}
            for i, j in enumerate(jobs)]


@pytest.fixture
def api(pg, tmp_path, monkeypatch, profile):
    profile_file = tmp_path / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    ranked_file = tmp_path / "ranked_jobs.json"
    for module, attr, value in [
            (deps, "PROFILE_FILE", profile_file), (deps, "RANKED_FILE", ranked_file),
            (rankings_router, "RANKED_FILE", ranked_file),
            (ranking, "PROFILE_FILE", profile_file),
            (ranking, "RANKED_FILE", ranked_file),
            (ranking, "_rank_jobs", _fake_rank)]:
        monkeypatch.setattr(module, attr, value)
    return TestClient(app), ranked_file


def _seed(n=3):
    corpus.upsert_jobs([_job(f"AI Engineer {i}") for i in range(n)])


def _worker():
    return Worker(lease_s=30)                      # default registry: "rank"


def _make_due(pg):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now()"))


def _task_count(pg):
    with pg.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM tasks")).scalar()


def test_second_post_while_a_rank_task_is_active_is_409_then_accepted(api, pg):
    c, _ = api
    _seed()
    first = c.post("/v1/rankings")
    assert first.status_code == 202
    assert first.json()["state"] == "running"
    second = c.post("/v1/rankings")
    assert second.status_code == 409
    assert second.json()["detail"] == "A ranking run is already in progress."

    assert _worker().run_once().outcome == "succeeded"
    assert c.post("/v1/rankings").status_code == 202      # key released
    assert _task_count(pg) == 2


def test_post_then_worker_writes_rankings_and_status_reports_done(api):
    c, ranked_file = api
    _seed(3)
    assert c.post("/v1/rankings").status_code == 202
    queued = c.get("/v1/rankings/status").json()
    assert queued["state"] == "running"
    assert "waiting for a worker" in queued["detail"]
    assert not ranked_file.exists()

    result = _worker().run_once()
    assert result.outcome == "succeeded"
    written = json.loads(ranked_file.read_text(encoding="utf-8"))
    assert [j["title"] for j in written] == [f"AI Engineer {i}" for i in range(3)]

    assert c.get("/v1/rankings/status").json() == {
        "state": "done", "detail": "", "ranked_count": 3}
    top = c.get("/v1/rankings?limit=2").json()             # payload unchanged
    assert [j["match_score"] for j in top] == [90.0, 89.0]

    task = taskqueue.get_task(result.task_id)
    assert task["result"]["ranked_count"] == 3
    assert task["result"]["formula_version"] == FORMULA_VERSION
    assert task["result"]["corpus_version"].startswith("db:3:")


def test_failing_rank_task_shows_retrying_then_failed(api, pg, monkeypatch):
    c, ranked_file = api
    _seed()

    def broken(profile, jobs):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(ranking, "_rank_jobs", broken)
    assert c.post("/v1/rankings").status_code == 202
    worker = _worker()
    assert worker.run_once().outcome == "failed"
    retrying = c.get("/v1/rankings/status").json()
    assert retrying["state"] == "running"
    assert "retrying" in retrying["detail"] and "attempt 1/3" in retrying["detail"]
    assert c.post("/v1/rankings").status_code == 409       # still active

    for _ in range(2):
        _make_due(pg)
        assert worker.run_once().outcome == "failed"
    failed = c.get("/v1/rankings/status").json()
    assert failed == {"state": "failed", "ranked_count": None,
                      "detail": "RuntimeError: embedding model unavailable"}
    assert not ranked_file.exists()
    assert c.post("/v1/rankings").status_code == 202       # dead frees the key


def test_post_with_an_empty_db_corpus_is_409_and_queues_nothing(api, pg):
    c, _ = api
    r = c.post("/v1/rankings")
    assert r.status_code == 409
    assert "corpus import" in r.json()["detail"]
    assert _task_count(pg) == 0


def test_post_without_a_profile_is_404(api, pg, monkeypatch, tmp_path):
    c, _ = api
    _seed()
    monkeypatch.setattr(deps, "PROFILE_FILE", tmp_path / "nope.json")
    assert c.post("/v1/rankings").status_code == 404
    assert _task_count(pg) == 0


def test_status_before_any_task_keeps_the_previous_run_behaviour(api):
    c, ranked_file = api
    assert c.get("/v1/rankings/status").json()["state"] == "idle"
    ranked_file.write_text("[]", encoding="utf-8")
    assert c.get("/v1/rankings/status").json() == {
        "state": "done", "detail": "loaded from previous run", "ranked_count": None}
