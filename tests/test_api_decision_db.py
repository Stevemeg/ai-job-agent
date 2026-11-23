"""POST /v1/applications/{id}/decision and the Phase 5 read side against a
real Postgres: approve / decline / edit through the API, 409 on a second
decision or a stale version, 422 for flagged edits, the inbox sorted by
score, the detail's packet / actions / approvals, and API_TOKEN."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402

from backend.agent import application_graph, decisions, run_graph, store  # noqa: E402
from backend.api.app import app                    # noqa: E402
from backend.database import corpus                # noqa: E402
from backend.worker import Worker                  # noqa: E402

pytestmark = pytest.mark.db

JD = "Build RAG pipelines with FAISS and LLMs. Pandas and Git required."


@pytest.fixture
def drafted(agent_env, monkeypatch):
    """Two applications awaiting approval (different scores) and a client."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    corpus.upsert_jobs([{"title": title, "company": "TestCo", "location": "Pune",
                         "tags": [], "seniority": "Junior", "clean_description": desc,
                         "apply_link": f"https://x/{i}", "source": "ats"}
                        for i, (title, desc) in enumerate([
                            ("AI Engineer", JD),
                            ("Data Analyst", "Excel reporting. Git a plus.")])])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    worker = Worker(lease_s=30)
    while worker.run_once() is not None:
        pass
    apps = store.list_applications(status="awaiting_approval", sort="score")
    assert len(apps) == 2
    agent_env.llm.calls.clear()
    return TestClient(app), apps


def _apply(n=1):
    worker = Worker({decisions.DECISION_KIND: application_graph.run_decision_task},
                    lease_s=30)
    for _ in range(n):
        run = worker.run_once()
        assert run is not None and run.outcome == "succeeded", run and run.error


def test_the_inbox_is_sorted_by_score(drafted):
    c, apps = drafted
    inbox = c.get("/v1/applications?status=awaiting_approval").json()
    scores = [a["match_score"] for a in inbox]
    assert scores == sorted(scores, reverse=True) and len(inbox) == 2
    assert all(a["status"] == "awaiting_approval" and a["version"] == 1 for a in inbox)
    newest = c.get("/v1/applications").json()
    assert [a["id"] for a in newest] == sorted((a["id"] for a in newest), reverse=True)
    assert [a["id"] for a in c.get("/v1/applications?status=awaiting_approval"
                                   "&sort=newest").json()] == sorted(
        (a["id"] for a in inbox), reverse=True)
    assert c.get("/v1/applications?sort=random").status_code == 422


def test_approve_through_the_api(drafted, agent_env):
    c, apps = drafted
    app_id = apps[0]["id"]
    before = c.get(f"/v1/applications/{app_id}").json()
    assert before["documents"]["cover_letter_paragraphs"]
    assert before["documents"]["resume_projects"][0]["bullets"]
    assert "explanation" in before["match"] and before["packet"] is None
    assert before["approvals"] == [] and before["actions"] == []

    r = c.post(f"/v1/applications/{app_id}/decision",
               json={"action": "approve", "version": 1, "reason": "Strong match"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["decision"], body["version"], body["source"]) == ("approve", 1, "user")
    again = c.post(f"/v1/applications/{app_id}/decision",
                   json={"action": "approve", "version": 1})
    assert again.status_code == 409 and "already has a decision" in again.json()["detail"]
    _apply()
    assert sum(agent_env.llm.calls.values()) == 0

    detail = c.get(f"/v1/applications/{app_id}").json()
    assert detail["status"] == "ready_to_apply"
    assert detail["packet"]["job"]["apply_link"] == detail["apply_link"]
    assert [(a["action"], a["status"]) for a in detail["actions"]] == [("packet", "done")]
    [approval] = detail["approvals"]
    assert (approval["decision"], approval["source"], approval["reason"]) == (
        "approve", "user", "Strong match")
    assert "packet" in {a["kind"] for a in detail["artifacts"]}
    assert c.get(f"/v1/applications/{app_id}/artifacts/packet").status_code == 200
    after = c.post(f"/v1/applications/{app_id}/decision",
                   json={"action": "decline", "version": 1})
    assert after.status_code == 409 and "ready_to_apply" in after.json()["detail"]
    with agent_env.pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM approvals")).scalar() == 1


def test_decline_and_a_stale_version_through_the_api(drafted, agent_env):
    c, apps = drafted
    app_id = apps[1]["id"]
    stale = c.post(f"/v1/applications/{app_id}/decision",
                   json={"action": "decline", "version": 2})
    assert stale.status_code == 409 and "reload" in stale.json()["detail"]
    r = c.post(f"/v1/applications/{app_id}/decision",
               json={"action": "decline", "version": 1, "reason": "Not my field"})
    assert r.status_code == 202
    _apply()
    detail = c.get(f"/v1/applications/{app_id}").json()
    assert detail["status"] == "declined" and detail["packet"] is None
    assert c.post("/v1/applications/999/decision",
                  json={"action": "approve", "version": 1}).status_code == 404
    bad = c.post(f"/v1/applications/{apps[0]['id']}/decision",
                 json={"action": "send", "version": 1})
    assert bad.status_code == 422


def test_edits_through_the_api(drafted, agent_env):
    c, apps = drafted
    app_id = apps[0]["id"]
    paragraphs = c.get(f"/v1/applications/{app_id}").json()["documents"][
        "cover_letter_paragraphs"]
    flagged = paragraphs[:2] + ["I shipped this at Google for 40,000 users."]
    r = c.post(f"/v1/applications/{app_id}/decision",
               json={"action": "edit", "version": 1,
                     "edits": {"cover_letter_paragraphs": flagged}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["flags"][0]["paragraph"] == 3
    assert {"Google", "40000"} <= set(detail["flags"][0]["flags"])
    malformed = c.post(f"/v1/applications/{app_id}/decision",
                       json={"action": "edit", "version": 1, "edits": {"subject": "x"}})
    assert malformed.status_code == 422 and "unknown edit keys" in malformed.json()["detail"]

    ok = c.post(f"/v1/applications/{app_id}/decision",
                json={"action": "edit", "version": 1, "acknowledge_flags": True,
                      "edits": {"cover_letter_paragraphs": flagged}})
    assert ok.status_code == 202, ok.text
    _apply()
    detail = c.get(f"/v1/applications/{app_id}").json()
    assert (detail["status"], detail["version"], detail["needs_review"]) == (
        "awaiting_approval", 2, True)
    assert detail["documents"]["cover_letter_paragraphs"][2] == flagged[2]
    assert detail["documents"]["edit_flags"][0]["paragraph"] == 3
    assert detail["approvals"][0]["edits"]["acknowledged_flags"]

    unflagged = flagged[:2]
    r = c.post(f"/v1/applications/{app_id}/decision",
               json={"action": "edit", "version": 2,
                     "edits": {"cover_letter_paragraphs": unflagged}})
    assert r.status_code == 202                       # no 422 without flags
    _apply()
    assert c.get(f"/v1/applications/{app_id}").json()["version"] == 3


def test_the_decision_endpoint_requires_the_token(drafted, monkeypatch):
    c, apps = drafted
    monkeypatch.setenv("API_TOKEN", "t0ken")
    url = f"/v1/applications/{apps[0]['id']}/decision"
    body = {"action": "approve", "version": 1}
    assert c.post(url, json=body).status_code == 401
    assert c.post(url, json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert decisions.list_approvals(apps[0]["id"]) == []
    r = c.post(url, json=body, headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 202
    assert c.get(f"/v1/applications/{apps[0]['id']}").status_code == 200   # reads stay open
