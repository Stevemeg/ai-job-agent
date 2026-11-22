"""/v1/scans end to end against a real Postgres: POST queues a scan, the
worker runs it (ATS fetchers replaced, embedding similarity stubbed), and
/latest reports its state and stats."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("requests")
pytest.importorskip("bs4")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402

from backend.api.app import app                    # noqa: E402
from backend.job_scraper import ats_collector      # noqa: E402
from backend.version import FORMULA_VERSION        # noqa: E402
from backend.worker import Worker                  # noqa: E402

pytestmark = pytest.mark.db


def _job(i):
    return {"title": f"AI Engineer {i}", "company": "acme", "location": "Remote",
            "tags": ["Engineering"], "seniority": "Junior",
            "clean_description": f"Role {i}: RAG with FAISS and LLMs.",
            "apply_link": f"https://acme/{i}", "source": "ats"}


@pytest.fixture
def api(pg, rank_files, monkeypatch, stub_semantic):
    boards = {("greenhouse", "acme"): [_job(i) for i in range(3)],
              ("ashby", "gone"): ats_collector.SourceFetchError("HTTP 404 (board not found)")}

    def fetcher(ats):
        def fetch(slug, strict=False):
            answer = boards[(ats, slug)]
            if isinstance(answer, Exception):
                raise answer
            return [dict(j) for j in answer]
        return fetch

    monkeypatch.setattr(ats_collector, "default_sources", lambda: list(boards))
    for ats in ("greenhouse", "ashby"):
        monkeypatch.setitem(ats_collector.FETCHERS, ats, fetcher(ats))
    return TestClient(app), boards


def test_post_queues_a_scan_and_latest_reports_its_stats(api, rank_files):
    c, _ = api
    assert c.get("/v1/scans/latest").json()["state"] == "idle"

    first = c.post("/v1/scans")
    assert first.status_code == 202
    body = first.json()
    assert body["state"] == "running" and body["trigger"] == "api"
    assert "waiting for a worker" in body["detail"] and body["result"] is None

    second = c.post("/v1/scans")
    assert second.status_code == 409
    assert second.json()["detail"] == "A scan is already in progress."

    assert Worker(lease_s=30).run_once().outcome == "succeeded"
    latest = c.get("/v1/scans/latest").json()
    assert (latest["state"], latest["task_id"]) == ("done", body["task_id"])
    assert latest["finished_at"] is not None
    stats = latest["result"]
    assert (stats["inserted"], stats["scored"], stats["skipped_unchanged"]) == (3, 3, 0)
    assert stats["sources_ok"] == 1
    assert stats["sources_failed"] == [{"source": "ashby", "slug": "gone",
                                        "error": "HTTP 404 (board not found)"}]
    assert stats["formula_version"] == FORMULA_VERSION
    assert stats["ranked_count"] == 3 and rank_files[1].exists()

    assert c.post("/v1/scans").status_code == 202          # key released
    assert Worker(lease_s=30).run_once().outcome == "succeeded"
    again = c.get("/v1/scans/latest").json()["result"]
    assert (again["inserted"], again["scored"], again["skipped_unchanged"]) == (0, 0, 3)


def test_latest_shows_retrying_then_failed_when_every_source_fails(api, pg):
    c, boards = api
    boards[("greenhouse", "acme")] = ConnectionError("network unreachable")
    assert c.post("/v1/scans").status_code == 202
    worker = Worker(lease_s=30)
    assert worker.run_once().outcome == "failed"
    retrying = c.get("/v1/scans/latest").json()
    assert retrying["state"] == "running"
    assert "retrying" in retrying["detail"] and "AllSourcesFailed" in retrying["detail"]
    assert c.post("/v1/scans").status_code == 409           # still active

    for _ in range(2):
        with pg.begin() as conn:
            conn.execute(text("UPDATE tasks SET run_after = now()"))
        assert worker.run_once().outcome == "failed"
    failed = c.get("/v1/scans/latest").json()
    assert failed["state"] == "failed" and failed["result"] is None
    assert failed["detail"].startswith("AllSourcesFailed: all 2 ATS sources failed")
    assert (failed["attempts"], failed["max_attempts"]) == (3, 3)
