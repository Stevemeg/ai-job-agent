"""The learning-to-rank export's labelling (backend/ltr/export.py), without
a database: the furthest stage wins, expiries are 0, telemetry is no row,
and the score features come from the decision-time snapshot."""
from datetime import datetime, timedelta, timezone

from backend.ltr import MIN_LABELLED_OUTCOMES
from backend.ltr.export import COLUMNS, LABELS, build_rows, to_csv

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _e(job, kind, minutes, source=None, score=None, breakdown=None, user=1):
    return {"user_id": user, "job_id": job, "event_type": kind, "source": source,
            "match_score": score, "score_breakdown": breakdown,
            "created_at": T0 + timedelta(minutes=minutes), "id": minutes,
            "company": f"c{job}", "title": f"Role {job}", "job_location": "Pune"}


BREAKDOWN = {"skill_overlap": 0.4, "semantic": 0.6, "role": 0.5, "seniority": 1.0}


def test_labels_are_the_furthest_stage_reached():
    events = [_e(1, "declined", 1, "user"),
              _e(2, "declined", 2, "expiry"),
              _e(3, "dismiss", 3),
              _e(4, "approved", 4, "user"),
              _e(5, "approved", 5, "user"), _e(5, "applied", 6),
              _e(6, "applied", 7), _e(6, "rejected", 8),
              _e(7, "applied", 9), _e(7, "interview", 10), _e(7, "rejected", 11),
              _e(8, "offer", 12),
              _e(9, "declined", 13, "user"), _e(9, "applied", 14),   # applied anyway
              _e(10, "approved", 15, "user")]
    rows = {r["job_id"]: r for r in build_rows(events, [])}
    assert {j: (r["label"], r["label_name"]) for j, r in rows.items()} == {
        1: (0, "declined"), 2: (0, "expired"), 3: (0, "dismiss"), 4: (1, "approved"),
        5: (2, "applied"), 6: (2, "applied"), 7: (3, "interview"), 8: (3, "offer"),
        9: (2, "applied"), 10: (1, "approved")}
    assert rows[7]["events"] == "applied>interview>rejected"
    assert rows[7]["label_at"] == (T0 + timedelta(minutes=10)).isoformat()
    assert rows[7]["first_event_at"] == (T0 + timedelta(minutes=9)).isoformat()
    assert rows[7]["last_event_at"] == (T0 + timedelta(minutes=11)).isoformat()
    assert LABELS == {"declined": 0, "expired": 0, "dismiss": 0, "approved": 1,
                      "applied": 2, "rejected": 2, "interview": 3, "offer": 3}


def test_telemetry_alone_is_no_row():
    events = [_e(1, "impression", 1), _e(1, "click", 2), _e(1, "save", 3),
              _e(1, "drafted", 4, "agent")]
    assert build_rows(events, []) == []


def test_features_come_from_the_decision_time_snapshot_then_the_application():
    events = [_e(1, "approved", 1, "user", 31.5, BREAKDOWN), _e(1, "applied", 2, None, 99, {}),
              _e(2, "applied", 3)]                                     # a manual log
    apps = [{"id": 7, "user_id": 1, "job_id": 2, "cache_key": "1:abc", "match_score": 22.0,
             "memory_effects": {"boost": 2.0, "memories": [{"id": 3}]},
             "location": "Remote, India", "score_breakdown": {**BREAKDOWN, "role": 0.9}}]
    rows = {r["job_id"]: r for r in build_rows(events, apps)}
    assert (rows[1]["match_score"], rows[1]["semantic"], rows[1]["application_id"]) == (
        31.5, 0.6, "")
    assert rows[1]["memory_boost"] == 0.0 and rows[1]["memory_effects"] == ""
    two = rows[2]
    assert (two["match_score"], two["role"], two["cache_key"], two["application_id"]) == (
        22.0, 0.9, "1:abc", 7)
    assert two["location"] == "Remote, India"
    assert two["memory_boost"] == 2.0
    assert two["memory_effects"] == '{"boost": 2.0, "memories": [{"id": 3}]}'


def test_the_csv_always_has_the_header():
    assert to_csv([]) == ",".join(COLUMNS) + "\n"
    text = to_csv(build_rows([_e(1, "offer", 1)], []))
    header, line = text.splitlines()
    assert header.split(",") == list(COLUMNS)
    assert line.startswith("1,1,,c1,Role 1,Pune,3,offer,")
    assert MIN_LABELLED_OUTCOMES == 50


def test_the_endpoint_needs_a_database():
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from backend.api.app import app
    r = TestClient(app).get("/v1/ltr/export")
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]


def test_the_cli_needs_a_database(capsys, tmp_path):
    from backend.ltr import export
    assert export.main(["--out", str(tmp_path / "x.csv")]) == 2
    assert "no database configured" in capsys.readouterr().err
    assert not (tmp_path / "x.csv").exists()
