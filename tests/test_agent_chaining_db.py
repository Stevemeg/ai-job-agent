"""A scan chains exactly one agent run when AGENT_ENABLED=true (also when
the scan task is retried), and none otherwise. No network: the ATS boards
are replaced, as in test_scan_db.py."""
import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("requests")
pytest.importorskip("bs4")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import run_graph               # noqa: E402
from backend.job_scraper import ats_collector     # noqa: E402
from backend.services import scan                 # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture
def boards(agent_env, monkeypatch):
    jobs = [{"title": f"AI Engineer {i}", "company": "acme", "location": "Bengaluru, India",
             "tags": [], "experience": "1 years", "seniority": "Junior",
             "clean_description": "RAG with FAISS and LLMs.",
             "apply_link": f"https://acme/{i}", "source": "ats",
             "postings": [{"source": "greenhouse", "posting_id": f"acme-{i}"}]}
            for i in range(2)]
    monkeypatch.setattr(ats_collector, "default_sources", lambda: [("greenhouse", "acme")])
    monkeypatch.setitem(ats_collector.FETCHERS, "greenhouse",
                        lambda slug, strict=False: [dict(j) for j in jobs])
    return agent_env


def _agent_runs(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT id, idempotency_key, payload FROM tasks WHERE kind = 'agent_run' "
            "ORDER BY id")).all()


def _scan_worker(handler=scan.run_scan_task):
    return Worker({scan.SCAN_KIND: handler}, lease_s=30)


def test_an_enabled_agent_is_chained_once_per_scan(boards, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    scan_id = scan.request_scan({"trigger": "api"})
    run = _scan_worker().run_once()
    assert run.outcome == "succeeded", run.error
    [agent_run] = _agent_runs(boards.pg)
    assert agent_run.idempotency_key == f"agent_run:scan:{scan_id}"
    assert agent_run.payload == {"trigger": "scan", "scan_task_id": scan_id}
    assert taskqueue.get_task(scan_id)["result"]["agent_run"] == {"task_id": agent_run.id}


def test_a_retried_scan_chains_no_second_run(boards, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    attempts = []

    def dies_after_chaining(payload, ctx):
        result = scan.run_scan_task(payload, ctx)       # chains the run...
        attempts.append(result["agent_run"])
        if len(attempts) == 1:
            raise RuntimeError("worker died before recording the scan")
        return result
    scan_id = scan.request_scan({"trigger": "api"})
    worker = _scan_worker(dies_after_chaining)
    assert worker.run_once().outcome == "failed"
    with boards.pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() WHERE kind = 'scan'"))
    assert worker.run_once().outcome == "succeeded"
    assert taskqueue.get_task(scan_id)["attempts"] == 2
    [agent_run] = _agent_runs(boards.pg)               # ...but only once
    assert attempts == [{"task_id": agent_run.id}, {"task_id": agent_run.id}]


def test_a_disabled_agent_is_never_chained(boards, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    scan_id = scan.request_scan({"trigger": "api"})
    assert _scan_worker().run_once().outcome == "succeeded"
    assert _agent_runs(boards.pg) == []
    assert "agent_run" not in taskqueue.get_task(scan_id)["result"]
    monkeypatch.delenv("AGENT_ENABLED")                 # the default: off
    scan.request_scan({"trigger": "api"})
    assert _scan_worker().run_once().outcome == "succeeded"
    assert _agent_runs(boards.pg) == []


def test_no_profile_no_chained_run(boards, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    boards.profile_file.unlink()
    scan.request_scan({"trigger": "api"})
    assert _scan_worker().run_once().outcome == "succeeded"
    assert _agent_runs(boards.pg) == []


def test_a_run_already_active_is_not_doubled(boards, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    manual = run_graph.request_agent_run(trigger="api")
    scan_id = scan.request_scan({"trigger": "api"})
    assert _scan_worker().run_once().outcome == "succeeded"
    assert [r.id for r in _agent_runs(boards.pg)] == [manual]
    assert taskqueue.get_task(scan_id)["result"]["agent_run"]["task_id"] is None
