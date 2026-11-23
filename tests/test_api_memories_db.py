"""GET/POST/PATCH/DELETE /v1/memories against a real Postgres: stated
memories, learned ones with their evidence, conflicts and validation, and
API_TOKEN on the mutating endpoints."""
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402

from backend.agent import memory, store            # noqa: E402
from backend.api import deps                       # noqa: E402
from backend.api.app import app                    # noqa: E402
from backend.database import corpus                # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture
def c(pg, profile, tmp_path, monkeypatch):
    path = tmp_path / "candidate_profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(deps, "PROFILE_FILE", path)
    monkeypatch.delenv("API_TOKEN", raising=False)
    for var in ("AGENT_LEARN_COMPANY_DECLINES", "AGENT_LEARN_TITLE_DECLINES",
                "AGENT_LEARN_PREFER_APPROVALS"):
        monkeypatch.delenv(var, raising=False)
    return TestClient(app)


def test_state_list_edit_deactivate_delete(c, profile):
    r = c.post("/v1/memories", json={"kind": "company_avoid", "value": "  Acme   Corp "})
    assert r.status_code == 201, r.text
    m = r.json()
    assert (m["kind"], m["value"], m["provenance"], m["active"], m["weight"]) == (
        "company_avoid", "Acme Corp", "stated", True, 2.0)
    assert m["evidence"] == {"stated_via": "api"}
    c.post("/v1/memories", json={"kind": "title_prefer", "value": "machine learning",
                                 "weight": 3})
    c.post("/v1/memories", json={"kind": "note", "value": "Prefer product companies."})
    listed = c.get("/v1/memories").json()
    assert [x["kind"] for x in listed] == ["company_avoid", "note", "title_prefer"]
    assert [x["kind"] for x in c.get("/v1/memories?kind=title_prefer").json()] == [
        "title_prefer"]

    r = c.patch(f"/v1/memories/{m['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    assert [x["id"] for x in c.get("/v1/memories?active=false").json()] == [m["id"]]
    r = c.patch(f"/v1/memories/{m['id']}", json={"value": "Acme", "weight": 4})
    assert (r.json()["value"], r.json()["weight"]) == ("Acme", 4.0)

    assert c.delete(f"/v1/memories/{m['id']}").status_code == 204
    assert c.delete(f"/v1/memories/{m['id']}").status_code == 404
    assert c.patch(f"/v1/memories/{m['id']}", json={"active": True}).status_code == 404


def test_conflicts_and_validation(c):
    body = {"kind": "company_avoid", "value": "Acme"}
    assert c.post("/v1/memories", json=body).status_code == 201
    r = c.post("/v1/memories", json={**body, "value": "ACME"})
    assert r.status_code == 409 and "already" in r.json()["detail"]
    other = c.post("/v1/memories", json={**body, "value": "Other"}).json()
    assert c.patch(f"/v1/memories/{other['id']}", json={"value": "acme"}).status_code == 409
    assert c.post("/v1/memories", json={"kind": "salary", "value": "x"}).status_code == 422
    assert c.post("/v1/memories", json={"kind": "title_avoid", "value": "--"}).status_code == 422
    assert c.post("/v1/memories", json={**body, "value": "Z", "weight": -1}).status_code == 422
    assert c.patch(f"/v1/memories/{other['id']}", json={}).status_code == 422
    assert c.get("/v1/memories?kind=nope").status_code == 422
    assert c.get("/v1/memories?provenance=guessed").status_code == 422


def test_learned_memories_are_listed_with_their_evidence(c, pg, profile):
    corpus.upsert_jobs([{"title": f"Role {i}", "company": "acme", "location": "Pune",
                         "tags": [], "clean_description": "x", "apply_link": "https://x",
                         "source": "ats"} for i in range(2)])
    user = store.user_for_profile(profile)
    with pg.begin() as conn:
        job_ids = conn.execute(text("SELECT id FROM canonical_jobs ORDER BY id")).scalars().all()
    for job_id in job_ids:
        made = store.dispatch(user_id=user, job_id=job_id, match_score=40,
                              profile_hash="p", cache_key="k")
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO approvals (application_id, decision, reason, source, "
                "application_version) VALUES (:a, 'decline', 'not for me', 'user', 1)"),
                {"a": made["application_id"]})
    memory.learn(user)
    [m] = c.get("/v1/memories?provenance=learned").json()
    assert (m["kind"], m["value"], m["weight"]) == ("company_avoid", "acme", 2.0)
    assert m["evidence"]["decline_reasons"] == ["not for me", "not for me"]
    assert len(m["evidence"]["declined"]) == 2
    # Editing its weight makes it yours; learning leaves it alone from then on.
    edited = c.patch(f"/v1/memories/{m['id']}", json={"weight": 1}).json()
    assert edited["provenance"] == "stated"
    memory.learn(user)
    assert c.get("/v1/memories?kind=company_avoid").json()[0]["weight"] == 1.0


def test_the_mutating_memory_endpoints_require_the_token(c, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    body = {"kind": "company_avoid", "value": "Acme"}
    assert c.post("/v1/memories", json=body).status_code == 401
    good = {"Authorization": "Bearer s3cret"}
    made = c.post("/v1/memories", json=body, headers=good)
    assert made.status_code == 201
    mid = made.json()["id"]
    for method, kwargs in (("patch", {"json": {"active": False}}), ("delete", {})):
        r = getattr(c, method)(f"/v1/memories/{mid}", **kwargs)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        r = getattr(c, method)(f"/v1/memories/{mid}",
                               headers={"Authorization": "Bearer wrong"}, **kwargs)
        assert r.status_code == 401
    assert c.get("/v1/memories").status_code == 200                # reads: no token
    assert c.patch(f"/v1/memories/{mid}", json={"active": False},
                   headers=good).status_code == 200
    assert c.delete(f"/v1/memories/{mid}", headers=good).status_code == 204
