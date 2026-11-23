"""/v1/agent/runs and /v1/applications end to end against a real Postgres:
POST queues a run, the worker runs it and the application it dispatches
(fake LLM, embedding similarity stubbed), and the read endpoints report
the result and serve the files."""
import hashlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from fastapi.testclient import TestClient          # noqa: E402

from backend.api.app import app                    # noqa: E402
from backend.database import corpus                # noqa: E402
from backend.worker import Worker                  # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture
def api(agent_env, monkeypatch):
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "TestCo",
                         "location": "Bengaluru, India", "tags": [], "seniority": "Junior",
                         "clean_description": "Build RAG pipelines with FAISS and LLMs.",
                         "apply_link": "https://x/1", "source": "ats"}])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    return TestClient(app)


def test_post_runs_and_the_application_endpoints(api, agent_env):
    c = api
    assert c.get("/v1/agent/runs/latest").json()["state"] == "idle"
    assert c.get("/v1/applications").json() == []

    first = c.post("/v1/agent/runs")
    assert first.status_code == 202
    body = first.json()
    assert (body["state"], body["trigger"], body["result"]) == ("running", "api", None)
    assert "waiting for a worker" in body["detail"]
    second = c.post("/v1/agent/runs")
    assert second.status_code == 409
    assert second.json()["detail"] == "An agent run is already in progress."

    worker = Worker(lease_s=30)
    assert worker.run_once().outcome == "succeeded"           # the run
    latest = c.get("/v1/agent/runs/latest").json()
    assert latest["state"] == "done" and latest["task_id"] == body["task_id"]
    result = latest["result"]
    assert result["triaged"] == 1 and result["skipped_existing"] == 0
    [dispatched] = result["dispatched"]
    assert dispatched["title"] == "AI Engineer" and dispatched["company"] == "TestCo"
    assert result["refresh"]["scored"] == 1 and result["cache_key"]

    [queued] = c.get("/v1/applications").json()
    assert queued["id"] == dispatched["application_id"]
    assert (queued["status"], queued["title"]) == ("queued", "AI Engineer")
    detail = c.get(f"/v1/applications/{queued['id']}").json()
    assert detail["artifacts"] == [] and detail["validator"] == {}
    assert detail["task"]["state"] == "running" and detail["task"]["attempts"] == 0
    assert c.get(f"/v1/applications/{queued['id']}/artifacts/resume_docx").status_code == 404

    assert worker.run_once().outcome == "succeeded"           # the application
    detail = c.get(f"/v1/applications/{queued['id']}").json()
    assert detail["status"] == "awaiting_approval" and detail["error"] is None
    assert (detail["needs_review"], detail["version"]) == (False, 1)
    assert detail["match_score"] == pytest.approx(dispatched["match_score"], abs=0.01)
    assert detail["thread_id"] == f"application:{queued['id']}"
    assert detail["task"]["state"] == "done"
    assert detail["validator"]["resume"]["ok"] is True
    assert detail["validator"]["cover_letter"] == {"ok": True, "paragraphs": []}
    kinds = {a["kind"]: a for a in detail["artifacts"]}
    assert set(kinds) == {"resume_docx", "cover_letter_docx", "cover_letter_txt",
                          "jd_requirements"}

    for kind, media in (("resume_docx", "wordprocessingml"),
                        ("cover_letter_txt", "text/plain"),
                        ("jd_requirements", "application/json")):
        r = c.get(kinds[kind]["download_url"])
        assert r.status_code == 200 and media in r.headers["content-type"]
        assert hashlib.sha256(r.content).hexdigest() == kinds[kind]["sha256"]
        assert r.headers["x-artifact-sha256"] == kinds[kind]["sha256"]
        assert len(r.content) == kinds[kind]["size_bytes"]
    assert r.content.startswith(b"{")
    assert "attachment" in c.get(kinds["resume_docx"]["download_url"]).headers[
        "content-disposition"]

    assert [a["id"] for a in c.get(
        "/v1/applications?status=awaiting_approval").json()] == [queued["id"]]
    assert c.get("/v1/applications?status=approved").json() == []
    assert c.get("/v1/applications?status=drafted").status_code == 422   # Phase 4 only
    assert c.get("/v1/applications?status=sent").status_code == 422
    assert c.get("/v1/applications/999").status_code == 404
    assert c.get(f"/v1/applications/{queued['id']}/artifacts/email").status_code == 404
    assert c.get("/v1/applications/999/artifacts/resume_docx").status_code == 404


def test_post_without_a_profile_is_409(api, agent_env):
    agent_env.profile_file.unlink()
    r = api.post("/v1/agent/runs")
    assert r.status_code == 409 and "No candidate profile" in r.json()["detail"]


def test_a_missing_file_is_404_not_500(api, agent_env):
    api.post("/v1/agent/runs")
    worker = Worker(lease_s=30)
    worker.run_once()
    worker.run_once()
    [app_row] = api.get("/v1/applications").json()
    (agent_env.apps / str(app_row["id"]) / "resume.docx").unlink()
    detail = api.get(f"/v1/applications/{app_row['id']}").json()
    assert {a["kind"]: a["size_bytes"] for a in detail["artifacts"]}["resume_docx"] is None
    r = api.get(f"/v1/applications/{app_row['id']}/artifacts/resume_docx")
    assert r.status_code == 404 and "no longer on disk" in r.json()["detail"]
