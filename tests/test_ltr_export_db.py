"""The learning-to-rank export against a real Postgres: the CLI and
GET /v1/ltr/export on a seeded database (labels, columns, features, memory
effects), and on an empty one (the header only)."""
import csv
import io
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402

from backend.agent import store                    # noqa: E402
from backend.api.app import app                    # noqa: E402
from backend.database import corpus                # noqa: E402
from backend.ltr import export                     # noqa: E402

pytestmark = pytest.mark.db


def _seed(pg):
    corpus.upsert_jobs([{"title": f"Role {i}", "company": f"co{i}", "location": "Pune",
                         "tags": [], "clean_description": "x", "apply_link": "https://x",
                         "source": "ats"} for i in range(6)])
    user = store.user_for_profile({"email": "u@example.com"})
    with pg.begin() as conn:
        ids = dict(conn.execute(text("SELECT title, id FROM canonical_jobs")).all())
        conn.execute(text(
            "INSERT INTO job_scores (job_id, profile_hash, cache_key, content_hash, passed, "
            "match_score, score_breakdown, explanation) VALUES (:j, 'p', '1:abc', 'c', true, "
            "27.5, CAST(:b AS jsonb), '{}'::jsonb)"),
            {"j": ids["Role 1"], "b": json.dumps({"skill_overlap": 0.3, "semantic": 0.7,
                                                  "role": 0.5, "seniority": 1.0})})
    made = store.dispatch(user_id=user, job_id=ids["Role 1"], match_score=27.5,
                          profile_hash="p", cache_key="1:abc",
                          memory_effects={"boost": 2.0, "memories": [
                              {"id": 9, "kind": "company_prefer", "value": "co1",
                               "provenance": "stated", "weight": 2.0}]})

    def event(job, kind, source=None, score=None, breakdown=None):
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO events (user_id, job_id, event_type, source, match_score, "
                "score_breakdown) VALUES (:u, :j, :t, :s, :m, CAST(:b AS jsonb))"),
                {"u": user, "j": ids[job], "t": kind, "s": source, "m": score,
                 "b": json.dumps(breakdown) if breakdown else None})
    event("Role 0", "declined", "user", 20, {"semantic": 0.4})
    event("Role 1", "drafted", "agent")
    event("Role 1", "approved", "user", 27.5)
    event("Role 1", "applied")
    event("Role 2", "declined", "expiry")
    event("Role 3", "applied")
    event("Role 3", "interview")
    event("Role 4", "impression")                     # telemetry: no row
    event("Role 5", "offer")
    return user, ids, made["application_id"]


def test_the_cli_and_the_endpoint_export_the_labelled_rows(pg, tmp_path, capsys,
                                                         monkeypatch):
    user, ids, app_id = _seed(pg)
    out = tmp_path / "ltr" / "outcomes.csv"
    assert export.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Wrote 5 labelled outcome(s)" in printed and "5/50" in printed
    text_csv = out.read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text_csv)))
    assert list(rows[0]) == list(export.COLUMNS)
    by_title = {r["title"]: r for r in rows}
    assert {t: (r["label"], r["label_name"]) for t, r in by_title.items()} == {
        "Role 0": ("0", "declined"), "Role 1": ("2", "applied"),
        "Role 2": ("0", "expired"), "Role 3": ("3", "interview"),
        "Role 5": ("3", "offer")}
    one = by_title["Role 1"]
    assert (one["application_id"], one["cache_key"], one["match_score"]) == (
        str(app_id), "1:abc", "27.5")
    assert (one["semantic"], one["seniority"]) == ("0.7", "1.0")   # from job_scores
    assert one["events"] == "approved>applied" and one["memory_boost"] == "2.0"
    assert json.loads(one["memory_effects"])["memories"][0]["kind"] == "company_prefer"
    assert by_title["Role 0"]["semantic"] == "0.4"                # the event's snapshot
    assert by_title["Role 0"]["application_id"] == ""
    assert all(r["user_id"] == str(user) for r in rows)

    monkeypatch.delenv("API_TOKEN", raising=False)
    r = TestClient(app).get("/v1/ltr/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-labelled-outcomes"] == "5" and r.headers["x-training-minimum"] == "50"
    assert "outcomes.csv" in r.headers["content-disposition"]
    assert r.text == text_csv


def test_an_empty_database_exports_the_header_only(pg, tmp_path, capsys, monkeypatch):
    out = tmp_path / "outcomes.csv"
    assert export.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == ",".join(export.COLUMNS) + "\n"
    assert "Wrote 0 labelled outcome(s)" in capsys.readouterr().out
    monkeypatch.delenv("API_TOKEN", raising=False)
    r = TestClient(app).get("/v1/ltr/export")
    assert r.status_code == 200 and r.text == ",".join(export.COLUMNS) + "\n"
    assert r.headers["x-labelled-outcomes"] == "0"
